"""Decides which fetch jobs are due; pure given its state and the game registry."""
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from gridiron_edge.budget import BudgetGuard

JobName = Literal["kalshi_refresh", "rundown_scores", "rundown_lines", "odds_games", "odds_props",
                  "cfbd_weekly", "nflverse_refresh", "settle", "scan", "expire"]
LEAGUES = ("nfl", "cfb")
# Player props: one "slate" pull per game from 3 h before kickoff (inside props_stale_seconds, so
# every game in a kickoff slot has fresh lines together), plus a refresh in the last hour only for
# games with an active PrizePicks idea or open entry.
PROPS_COST = 3  # credits per pull: 3 markets x 1 region (odds_api.PROPS_COST)
GAME_ODDS_COST = 3  # credits per game-odds pull (odds_api.GAME_ODDS_COST)
PROPS_SLATE_LEAD = timedelta(hours=3)
PROPS_REFRESH_LEAD = timedelta(minutes=60)
PROPS_RETRY = timedelta(minutes=15)  # a props pull that didn't run (budget, game not listed) retries after
# Budget (500 free credits a month): NFL props first (~16 games x 3 = ~48 credits a week). CFB props
# only while the credits left stay above PROPS_CFB_RESERVE, kept for game-odds pulls (early/mid
# windows and closing lines, ~120 a month), plus the NFL props still to come before the monthly
# reset (assumed the 1st, UTC). The guard's own 10% floor applies on top. CFB props wait until the
# quota is known (the first paid pull records it). NFL last-hour refresh pulls are not in the
# weekly estimate; they run only for games with a PrizePicks idea, so a few a week at most.
PROPS_CFB_RESERVE = 120
NFL_PROPS_PER_WEEK = 16 * PROPS_COST
SCAN_DEBOUNCE = timedelta(seconds=60)
# Weekday numbers: Mon=0 ... Sun=6. (weekday, hour) of each league's odds-window anchors.
_EARLY = {"nfl": (1, 9), "cfb": (6, 12)}
_MID = {"nfl": (3, 9), "cfb": (2, 9)}
_SCAN_TRIGGERS = ("kalshi_refresh", "odds_games", "odds_props", "rundown_lines")
# TheRundown pacing: live scores every minute only while money rides on a live game.
LIVE_BET_SCORES_EVERY = timedelta(seconds=60)
LIVE_SCORES_EVERY = timedelta(minutes=5)
RUNDOWN_LINES_EVERY = timedelta(minutes=30)
LINES_HORIZON = timedelta(hours=48)
# Odds API closing pulls: one per league per fixed 2-hour UTC block that holds a game we care about.
CLOSE_SLOP = timedelta(minutes=5)  # kickoff times differ slightly between providers


def _odds_time(t: datetime) -> str:
    """The Odds API's documented commence-time form: UTC with a literal Z."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cfb_props_floor(now: datetime) -> int:
    """Credits that must stay after a CFB props pull: the game-odds reserve plus the NFL props
    still expected before the quota resets on the 1st."""
    t = now.astimezone(UTC)
    nxt = datetime(t.year + (t.month == 12), t.month % 12 + 1, 1, tzinfo=UTC)
    weeks_left = (nxt - t).total_seconds() / timedelta(weeks=1).total_seconds()
    return PROPS_CFB_RESERVE + math.ceil(weeks_left * NFL_PROPS_PER_WEEK)


def props_fit(guard: BudgetGuard | None, spend: int, floor: int | None = None) -> bool:
    """Whether `spend` credits of props pulls (cumulative) fit: within the guard and, when `floor` is
    given, leaving at least `floor` credits. An unknown quota never clears a floor."""
    if guard is None:
        return True
    if not guard.can_spend(spend):
        return False
    if floor is None:
        return True
    rem = guard.remaining()
    return rem is not None and rem - spend >= floor


def _in_close_window(kickoff: datetime, now: datetime) -> bool:
    return kickoff - timedelta(minutes=20) <= now <= kickoff - timedelta(minutes=5)


@dataclass
class Job:
    name: JobName
    league: str | None = None
    args: dict = field(default_factory=dict)
    essential: bool = False

    @property
    def key(self) -> tuple:
        return (self.name, self.league, tuple(sorted(self.args.items())))


def _last_at_or_before(now: datetime, weekday: int, hour: int) -> datetime:
    """Most recent local weekday/hour at or before `now` (now is already in local tz)."""
    anchor = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    anchor -= timedelta(days=(now.weekday() - weekday) % 7)
    if anchor > now:
        anchor -= timedelta(days=7)
    return anchor


class Scheduler:
    def __init__(self, registry, budgets: dict[str, BudgetGuard], state: dict | None = None,
                 tz=ZoneInfo("America/Los_Angeles"),
                 has_open_bets: Callable[[], bool] = lambda: False,
                 open_bet_games: Callable[[], set[str]] = lambda: set(),
                 close_games: Callable[[], set[str]] = lambda: set(),
                 pp_games: Callable[[], set[str]] = lambda: set()):
        """open_bet_games: game ids with a PLACED bet. close_games: game ids whose closing line
        matters (a PLACED bet or a live, non-observe recommendation). pp_games: game ids with an
        active PrizePicks idea or open entry (their prop lines are refreshed near kickoff)."""
        self.registry = registry
        self.budgets = budgets
        self.state: dict = {} if state is None else state
        self.tz = tz
        self.has_open_bets = has_open_bets
        self.open_bet_games = open_bet_games
        self.close_games = close_games
        self.pp_games = pp_games
        self._scan_pending = False
        self._retry_at: dict[tuple, datetime] = {}  # props pulls that didn't run: not before this

    # -- recording -------------------------------------------------------
    def mark_done(self, job: Job, now: datetime) -> None:
        if self._retry_at.get(job.key, now) > now:
            return  # deferred while running (skipped for budget or game not listed): try again later
        self._retry_at.pop(job.key, None)
        self.state[job.key] = now
        if job.name == "scan":
            self._scan_pending = False
        elif job.name in _SCAN_TRIGGERS:
            self._scan_pending = True

    def defer(self, job: Job, until: datetime) -> None:
        """The job ran but did nothing: leave it undone and don't offer it again before `until`."""
        self._retry_at[job.key] = until

    def request_scan(self, now: datetime) -> None:
        self._scan_pending = True

    # -- helpers ---------------------------------------------------------
    def _elapsed(self, job: Job, now: datetime, every: timedelta) -> bool:
        last = self.state.get(job.key)
        return last is None or now - last >= every

    def _done_since(self, job: Job, since: datetime) -> bool:
        last = self.state.get(job.key)
        return last is not None and last >= since

    # -- rules -----------------------------------------------------------
    def due(self, now: datetime) -> list[Job]:
        local = now.astimezone(self.tz)
        upcoming = [g for g in self.registry.upcoming() if g.kickoff >= now]
        live = list(self.registry.live())
        out: list[Job] = []

        def add(job: Job, every: timedelta) -> None:
            if self._elapsed(job, now, every):
                out.append(job)

        add(Job("expire"), timedelta(seconds=60))

        bet_games = set(self.open_bet_games()) if live else set()
        for league in LEAGUES:
            lg_up = [g for g in upcoming if g.league == league]
            lg_live = [g for g in live if g.league == league]
            if any(g.game_id in bet_games for g in lg_live):
                every = LIVE_BET_SCORES_EVERY
            elif lg_live:
                every = LIVE_SCORES_EVERY
            elif any(g.kickoff - now <= timedelta(hours=3) for g in lg_up):
                every = timedelta(minutes=15)
            else:
                every = timedelta(hours=1)
            add(Job("rundown_scores", league), every)
            if any(g.kickoff - now <= LINES_HORIZON for g in lg_up):
                add(Job("rundown_lines", league), RUNDOWN_LINES_EVERY)

        add(Job("kalshi_refresh"), self._kalshi_every(now, upcoming, live))

        games = self._odds_games(now, local, upcoming)
        out += games
        out += self._odds_props(now, local, upcoming, committed=GAME_ODDS_COST * len(games))

        if self.has_open_bets():
            add(Job("settle"), timedelta(minutes=5))

        tue6 = _last_at_or_before(local, 1, 6)
        for name in ("cfbd_weekly", "nflverse_refresh"):
            if not self._done_since(Job(name), tue6):
                out.append(Job(name))

        scan = Job("scan")
        if self._scan_pending and self._elapsed(scan, now, SCAN_DEBOUNCE):
            out.append(scan)
        return out

    @staticmethod
    def _kalshi_every(now: datetime, upcoming, live) -> timedelta:
        near = live or [g for g in upcoming if g.kickoff - now <= timedelta(hours=24)]
        return timedelta(minutes=3 if near else 30)

    def next_check(self, now: datetime) -> datetime:
        """When the next Kalshi price refresh (and the scan it triggers) is due."""
        upcoming = [g for g in self.registry.upcoming() if g.kickoff >= now]
        last = self.state.get(Job("kalshi_refresh").key)
        if last is None:
            return now
        return max(now, last + self._kalshi_every(now, upcoming, list(self.registry.live())))

    def _odds_games(self, now: datetime, local: datetime, upcoming) -> list[Job]:
        out: list[Job] = []
        wanted = None  # computed lazily: only when some game is near its close window
        for league in LEAGUES:
            lg_up = [g for g in upcoming if g.league == league]
            if not lg_up:
                continue
            start = _last_at_or_before(local, *_EARLY[league])
            mid = _first_at_or_after(start, *_MID[league])
            for window, anchor in (("early", start), ("mid", mid)):
                job = Job("odds_games", league, {"window": window})
                if local >= anchor and not self._done_since(job, anchor):
                    out.append(job)
            if not any(_in_close_window(g.kickoff, now) for g in lg_up):
                continue
            if wanted is None:
                wanted = set(self.close_games())
            # Closing lines only matter for CLV and last-minute recs: one pull per distinct kickoff
            # that holds a game with a PLACED bet or an active recommendation, keyed by kickoff so
            # it runs once. CLV is recorded for bets kicking off within 30 min of the pull.
            for kickoff in sorted({g.kickoff for g in lg_up if g.game_id in wanted}):
                if not _in_close_window(kickoff, now):
                    continue
                job = Job("odds_games", league, {
                    "window": f"close:{_odds_time(kickoff)}",
                    "commence_from": _odds_time(kickoff - CLOSE_SLOP),
                    "commence_to": _odds_time(kickoff + CLOSE_SLOP),
                }, essential=league == "nfl")
                if job.key not in self.state:
                    out.append(job)
        return out

    def _odds_props(self, now: datetime, local: datetime, upcoming, committed: int = 0) -> list[Job]:
        """committed: credits the game-odds jobs due in this same tick will spend."""
        guard = self.budgets.get("odds_api")
        floor = cfb_props_floor(now)
        out: list[Job] = []

        def add(job: Job) -> None:
            if self._retry_at.get(job.key, now) > now:
                return
            spend = committed + PROPS_COST * (len(out) + 1)
            if props_fit(guard, spend, floor if job.league == "cfb" else None):
                out.append(job)

        def slate(g) -> Job:
            return Job("odds_props", g.league, {"game_id": g.game_id, "window": "slate"})

        for g in sorted((g for g in upcoming if now < g.kickoff <= now + PROPS_SLATE_LEAD),
                        key=lambda g: (LEAGUES.index(g.league), g.kickoff)):
            if slate(g).key not in self.state:  # a late start still pulls once, up to kickoff
                add(slate(g))
        near = [g for g in upcoming if now < g.kickoff <= now + PROPS_REFRESH_LEAD]
        if not near:
            return out
        wanted = set(self.pp_games())  # lazily: only when a game is in its last hour
        for g in sorted(near, key=lambda g: (LEAGUES.index(g.league), g.kickoff)):
            pulled = self.state.get(slate(g).key)
            if g.game_id not in wanted or pulled is None or pulled > g.kickoff - PROPS_REFRESH_LEAD:
                continue  # never pulled, or pulled within the last hour already
            job = Job("odds_props", g.league, {"game_id": g.game_id, "window": "refresh"})
            if job.key not in self.state:
                add(job)
        return out


def _first_at_or_after(t: datetime, weekday: int, hour: int) -> datetime:
    anchor = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    anchor += timedelta(days=(weekday - anchor.weekday()) % 7)
    if anchor < t:
        anchor += timedelta(days=7)
    return anchor
