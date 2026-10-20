"""Decides which fetch jobs are due; pure given its state and the game registry."""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from gridiron_edge.budget import BudgetGuard

JobName = Literal["kalshi_refresh", "rundown_scores", "rundown_lines", "odds_games", "odds_props",
                  "cfbd_weekly", "nflverse_refresh", "settle", "scan", "expire"]
LEAGUES = ("nfl", "cfb")
PROPS_WEEKLY_CAP = 8
PROPS_COST = 3
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
CLOSE_BLOCK = timedelta(hours=2)


def _odds_time(t: datetime) -> str:
    """The Odds API's documented commence-time form: UTC with a literal Z."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_close_window(kickoff: datetime, now: datetime) -> bool:
    return kickoff - timedelta(minutes=20) <= now <= kickoff - timedelta(minutes=5)


def _block_start(t: datetime) -> datetime:
    u = t.astimezone(UTC)
    return u.replace(hour=u.hour - u.hour % 2, minute=0, second=0, microsecond=0)


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
                 close_games: Callable[[], set[str]] = lambda: set()):
        """open_bet_games: game ids with a PLACED bet. close_games: game ids whose closing line
        matters (a PLACED bet or a live, non-observe recommendation)."""
        self.registry = registry
        self.budgets = budgets
        self.state: dict = {} if state is None else state
        self.tz = tz
        self.has_open_bets = has_open_bets
        self.open_bet_games = open_bet_games
        self.close_games = close_games
        self._scan_pending = False

    # -- recording -------------------------------------------------------
    def mark_done(self, job: Job, now: datetime) -> None:
        self.state[job.key] = now
        if job.name == "scan":
            self._scan_pending = False
        elif job.name in _SCAN_TRIGGERS:
            self._scan_pending = True

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

        near = live or [g for g in upcoming if g.kickoff - now <= timedelta(hours=24)]
        add(Job("kalshi_refresh"), timedelta(minutes=3 if near else 30))

        out += self._odds_games(now, local, upcoming)
        out += self._odds_props(now, local, upcoming)

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
            # Closing lines only matter for CLV and last-minute recs: one pull per 2-hour block
            # that holds a game with a PLACED bet or an active recommendation, timed to the
            # block's first such kickoff. The args depend only on the block, so it runs once.
            blocks: dict[datetime, list[datetime]] = {}
            for g in lg_up:
                if g.game_id in wanted:
                    blocks.setdefault(_block_start(g.kickoff), []).append(g.kickoff)
            for block, kickoffs in sorted(blocks.items()):
                first = min(kickoffs)
                if not _in_close_window(first, now):
                    continue
                job = Job("odds_games", league, {
                    "window": f"close:{_odds_time(block)}",
                    "commence_from": _odds_time(block),
                    "commence_to": _odds_time(block + CLOSE_BLOCK),
                }, essential=league == "nfl")
                if job.key not in self.state:
                    out.append(job)
        return out

    def _odds_props(self, now: datetime, local: datetime, upcoming) -> list[Job]:
        guard = self.budgets.get("odds_api")
        if guard is not None and not guard.can_spend(PROPS_COST):
            return []
        week = local.isocalendar()[:2]
        used = sum(1 for k, t in self.state.items()
                   if k[0] == "odds_props" and t.astimezone(self.tz).isocalendar()[:2] == week)
        cands = sorted((g for g in upcoming if g.kickoff - now <= timedelta(hours=2)),
                       key=lambda g: (LEAGUES.index(g.league), g.kickoff))
        out: list[Job] = []
        for g in cands:
            if used + len(out) >= PROPS_WEEKLY_CAP:
                break
            job = Job("odds_props", g.league, {"game_id": g.game_id})
            if job.key not in self.state:
                out.append(job)
        return out


def _first_at_or_after(t: datetime, weekday: int, hour: int) -> datetime:
    anchor = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    anchor += timedelta(days=(weekday - anchor.weekday()) % 7)
    if anchor < t:
        anchor += timedelta(days=7)
    return anchor
