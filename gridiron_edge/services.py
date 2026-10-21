"""Wires settings, db, clients, models, notifier and scheduler together; runs startup and fetch jobs."""
import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import httpx
import numpy as np
import polars as pl

from gridiron_edge import pipeline
from gridiron_edge.bets.lifecycle import Lifecycle
from gridiron_edge.bets.settle import Settler
from gridiron_edge.budget import BudgetGuard
from gridiron_edge.clients import odds_api, therundown
from gridiron_edge.clients.cfbd import BASE_URL as CFBD_URL
from gridiron_edge.clients.cfbd import CfbdClient
from gridiron_edge.clients.http import ApiError, AuthError, RateLimitedClient
from gridiron_edge.clients.kalshi import GAME_SERIES, PROP_SERIES_STAT, KalshiClient
from gridiron_edge.clients.nflverse import NflverseSource
from gridiron_edge.clients.odds_api import OddsApiClient
from gridiron_edge.clients.therundown import TheRundownClient
from gridiron_edge.config import Settings, apply_override
from gridiron_edge.db import Ledger, connect, register_schema
from gridiron_edge.matching import GameRegistry, TeamResolver, kalshi_resolver, normalize_name
from gridiron_edge.models import Prediction
from gridiron_edge.models import cfb as cfb_models
from gridiron_edge.models import nfl as nfl_models
from gridiron_edge.models.blend import BlendFit, walkforward_ats, walkforward_totals
from gridiron_edge.models.cfb import CfbRatings, cfb_prior, games_played
from gridiron_edge.models.nfl import NflRatings, is_neutral, qb_changes
from gridiron_edge.models.props import STAT_COLUMNS, PropProjector
from gridiron_edge.models.ridge import implied_ratings
from gridiron_edge.notify.chain import NotifyChain
from gridiron_edge.notify.console import ConsoleNotifier
from gridiron_edge.notify.format import PT, format_message
from gridiron_edge.notify.imessage import IMessageNotifier
from gridiron_edge.notify.ntfy import NtfyNotifier
from gridiron_edge.scheduler import LEAGUES, Job, Scheduler
from gridiron_edge.strategy.distributions import (
    CFB_SIGMA, MIN_EMPIRICAL_RESIDUALS, NFL_ML_SIGMA, NFL_SPREAD_SIGMA, NFL_TOTAL_SIGMA,
    ConditionalMarginModel, MarginModel, NormalModel,
)
from gridiron_edge.strategy.kelly import clv_throttle
from gridiron_edge.strategy.prizepicks import payout_table
from gridiron_edge.types import Game, KalshiMarket, Leg, PropQuote, Quote, Recommendation, make_game_id
from gridiron_edge.ui.widgets import compute_kpis

register_schema("CREATE TABLE IF NOT EXISTS scheduler_state (key TEXT PRIMARY KEY, ts TEXT NOT NULL)")

STATE_TTL = timedelta(days=14)
UPCOMING_WINDOW = timedelta(days=8)
GATE_REFIT = timedelta(days=7)
GATE_RECENT = timedelta(days=1)  # refits closer together than this don't count as another pass
GATE_PASSES = 2  # consecutive passing refits before a market goes live
QUOTE_TTL = timedelta(days=2)
LINES_HORIZON = timedelta(hours=48)
# sportsbook quotes and prop lines, written through to api_cache so a restart keeps them
_PERSISTED = {"quotes": Quote, "props": PropQuote}
NFL_SEASONS = 3
MARGIN_FIRST_SEASON = 2015  # spread-conditional margin models fit on closing lines from here on
CFB_MARGIN_KEY = "cfb_spread"
# Settings the `set` command may not touch: secrets and paths live in .env / --db.
LOCKED_SETTINGS = {"odds_api_key", "cfbd_api_key", "therundown_api_key", "notify_phone", "ntfy_topic",
                   "kalshi_base_url", "db_path"}
SCAN_KINDS = {"games": {"kalshi_game"}, "props": {"kalshi_prop"}, "pp": {"pp_entry"}, "arb": {"arb", "middle"}}
NOTIFY_TEST_TEXT = "GE test - if your phone did not alert, enable ntfy (NTFY_TOPIC in .env)"


class _SchedulerState(dict):
    """Scheduler run times, written through to sqlite so a restart doesn't re-spend API credits."""

    def __init__(self, conn: sqlite3.Connection, now: datetime):
        super().__init__()
        self.conn = conn
        conn.execute("DELETE FROM scheduler_state WHERE ts < ?", ((now - STATE_TTL).astimezone(UTC).isoformat(),))
        for r in conn.execute("SELECT key, ts FROM scheduler_state"):
            name, league, args = json.loads(r["key"])
            super().__setitem__((name, league, tuple(tuple(a) for a in args)), datetime.fromisoformat(r["ts"]))

    def __setitem__(self, key, ts: datetime) -> None:
        super().__setitem__(key, ts)
        self.conn.execute("INSERT OR REPLACE INTO scheduler_state (key, ts) VALUES (?,?)",
                          (json.dumps(key), ts.astimezone(UTC).isoformat()))


class _RegistryView:
    """The game registry as the Scheduler asks for it: upcoming() and live() with no arguments."""

    def __init__(self, registry: GameRegistry, clock: Callable[[], datetime]):
        self.registry = registry
        self.clock = clock

    def upcoming(self) -> list[Game]:
        return self.registry.upcoming(UPCOMING_WINDOW, self.clock())

    def live(self) -> list[Game]:
        return self.registry.live()


class _NoProjector:
    """Used until nflverse weekly stats load: no projections, no teams."""

    def project(self, player, stat, season, asof_week):
        return None

    def team_of(self, player):
        return None


def _season(kickoff: datetime) -> int:
    return kickoff.year if kickoff.month >= 7 else kickoff.year - 1


def _season_of_date(d: date) -> int:
    return d.year if d.month >= 7 else d.year - 1


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _league_of(m: KalshiMarket) -> str:
    return "cfb" if m.series.startswith("KXNCAAF") else "nfl"


_STAMPS = ("last_update", "fetched_at")


def _dump_items(items: list) -> str:
    return json.dumps([{k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in asdict(i).items()}
                       for i in items])


def _seen_at(item) -> datetime:
    """When we last pulled an item (falls back to the book's own stamp)."""
    return getattr(item, "fetched_at", None) or item.last_update


def _load_items(cls, body: str) -> list:
    out = []
    for d in json.loads(body):
        try:
            out.append(cls(**{**d, **{k: datetime.fromisoformat(d[k]) for k in _STAMPS if d.get(k)}}))
        except (KeyError, TypeError, ValueError):
            continue  # an older row shape: drop it rather than fail startup
    return out


def _bet_legs(bets) -> list[dict]:
    return [leg for b in bets for leg in json.loads(b["legs_json"])]


def nfl_margin_model(sched: pl.DataFrame) -> MarginModel:
    """P(margin | closing spread) from nflverse; spread_line is the expected home margin."""
    done = sched.filter(pl.col("season") >= MARGIN_FIRST_SEASON).drop_nulls(["result", "spread_line"])
    if done.height < MIN_EMPIRICAL_RESIDUALS:
        return NormalModel(NFL_SPREAD_SIGMA)
    return ConditionalMarginModel(done["spread_line"].cast(pl.Float64).to_numpy(),
                                  done["result"].cast(pl.Float64).to_numpy(), NFL_SPREAD_SIGMA)


def _default_margin_models() -> dict:
    return {"nfl_spread": NormalModel(NFL_SPREAD_SIGMA), "nfl_total": NormalModel(NFL_TOTAL_SIGMA),
            "nfl_ml": NormalModel(NFL_ML_SIGMA), "cfb": NormalModel(CFB_SIGMA)}


def _describe(rec: Recommendation) -> str:
    legs = " + ".join(leg.description for leg in rec.legs)
    tail = " (observe-only)" if rec.observe_only else f" stake ${rec.stake_cents / 100:,.2f}"
    return f"#{rec.id} {rec.kind} {legs} EV {rec.ev:+.1%}{tail}"


def _register(registry: GameRegistry, teams: TeamResolver, league: str, home_name: str,
              away_name: str, kickoff: datetime) -> str | None:
    """The game's id, adding it to the registry when both teams are known but the game is not."""
    home, away = teams.canonical(league, home_name), teams.canonical(league, away_name)
    if home is None or away is None:
        return None
    game_id = registry.resolve(league, home, away, kickoff)
    if game_id is None:
        game_id = make_game_id(league, kickoff.astimezone(UTC), away, home)
        registry.upsert(Game(game_id, league, _season(kickoff), None, kickoff, home, away))
    return game_id


def _http(transports: dict, name: str, base_url: str, headers: dict | None = None,
          rate: float = 2.0) -> RateLimitedClient | None:
    """A client on an injected transport (tests); None lets the API client build its own."""
    t = transports.get(name)
    return None if t is None else RateLimitedClient(base_url, headers=headers, rate=rate, transport=t)


def _dated(sched: pl.DataFrame) -> pl.DataFrame:
    """nflverse ships gameday as text; the models need a Date column."""
    if "gameday" in sched.columns and sched["gameday"].dtype == pl.String:
        return sched.with_columns(pl.col("gameday").str.to_date(strict=False))
    return sched


def _nfl_history(sched: pl.DataFrame, before: date) -> list[dict]:
    """Completed NFL games with a closing line, as walkforward_ats rows (home_line = -spread_line)."""
    out = []
    for r in sched.drop_nulls(["result", "spread_line", "gameday"]).iter_rows(named=True):
        d = _as_date(r["gameday"])
        if d < before:
            out.append({"date": d, "home": r["home_team"], "away": r["away_team"],
                        "neutral": is_neutral(r), "season": r.get("season"),
                        "home_line": -float(r["spread_line"]), "margin": float(r["result"])})
    return out


# where the model hyperparameters came from (recorded with each gate fit)
HYPERPARAMS = {
    "nfl": f"lambda={nfl_models.LAMBDA}, half-life={nfl_models.HALF_LIFE_DAYS:g}d, "
           f"pts/epa/market={nfl_models.W_PTS}/{nfl_models.W_EPA}/{nfl_models.W_MKT}: hand-set from brief1 §5, not tuned",
    "cfb": f"margin cap={cfb_models.MARGIN_CAP:g}, weekly decay={cfb_models.WEEKLY_DECAY}, lambda=max(2, 20-2*week), "
           f"prior 0.6/0.25/0.15, market share={cfb_models.W_MARKET}: hand-set from brief1 §5, not tuned",
    "sigma": "Normal 13 (NFL) / 15.75 (CFB); NFL uses the empirical close-residual model once >= 500 residuals",
}


_log = logging.getLogger(__name__)
_GATE_LABEL = {"nfl": "NFL spreads/moneylines", "cfb": "College football bets"}


def _gate_label(league: str) -> str:
    return _GATE_LABEL.get(league, f"{league.upper()} bets")


def _gate_line(label: str, r: dict) -> str:
    """Plain-English gate summary for the Log tab; b_model/se stay in the stored JSON only."""
    verdict = ("not yet trusted, bets are shown but not texted" if r["gated"]
               else "trusted, bets are texted")
    return f"{label}: model checked on {r['n']} past games; {verdict}{_gate_detail(r)}"


def _gate_detail(r: dict) -> str:
    """A plain-English summary of a gate fit: simulated return, early vs later season, why not live yet."""
    parts = []
    if "sim_roi" in r:
        parts.append(f"simulated return {r['sim_roi']:+.1%} on {r.get('sim_bets', 0)} bets after Kalshi fees")
    if "early" in r and "late" in r:
        parts.append(f"right {r['early']['ats_pct']:.0%} of the time in the first 4 weeks, "
                     f"{r['late']['ats_pct']:.0%} after")
    if r.get("passed") and r.get("gated"):
        parts.append(f"passed {r.get('streak', 0)} of the {GATE_PASSES} weekly checks needed to go live")
    return f" ({'; '.join(parts)})" if parts else ""


@dataclass
class Services:
    settings: Settings
    conn: sqlite3.Connection
    ledger: Ledger
    lifecycle: Lifecycle
    settler: Settler
    teams: TeamResolver
    registry: GameRegistry
    budgets: dict[str, BudgetGuard]
    scheduler: Scheduler
    notifier: NotifyChain
    odds: OddsApiClient | None
    kalshi: KalshiClient
    rundown: TheRundownClient | None
    cfbd: CfbdClient | None
    nflverse: NflverseSource
    projector: pipeline.Projector = field(default_factory=_NoProjector)
    nfl_model: NflRatings | None = None
    cfb_model: CfbRatings | None = None
    gates: dict[tuple[str, str], bool] = field(default_factory=dict)
    gates_complete: bool = False  # last fit_gates backtested every league
    blend_fits: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    disagree_caps: dict[str, float] = field(default_factory=dict)
    qb_changes: set[str] = field(default_factory=set)  # NFL teams with a new depth-chart QB1
    _reported_unmatched: set[str] = field(default_factory=set)
    projector_live: bool = False  # projector-only props stake only after its calibration check passes
    margin_models: dict = field(default_factory=_default_margin_models)
    latest: dict = field(default_factory=lambda: {"quotes": [], "props": [], "kalshi": [], "stats": None})
    player_teams: dict[str, str] = field(default_factory=dict)
    manual_props: list[PropQuote] = field(default_factory=list)
    manual_teams: dict[str, str] = field(default_factory=dict)
    history: dict[str, pl.DataFrame] = field(default_factory=dict)  # "nfl" schedules, "nfl_epa"
    console_sink: list[str] = field(default_factory=list)
    kalshi_ok: bool = False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _auth_reported: set[str] = field(default_factory=set)

    # -- settings table --------------------------------------------------
    def _get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def _put(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

    def set_setting(self, name: str, raw: str) -> Settings:
        """The `set` command: validate, apply, and persist one strategy/behaviour setting."""
        if name in LOCKED_SETTINGS:
            raise ValueError(f"{name} comes from .env or --db, not set")
        self.settings = apply_override(self.settings, name, raw)
        self._put(f"set.{name}", str(raw))
        self.notifier.enabled = self.settings.notify_enabled
        self.notifier.quiet_hours = self.settings.quiet_hours
        self.settler.payouts = payout_table(self.settings)  # pp_mode / pp_payouts_json changes
        return self.settings

    # -- error handling --------------------------------------------------
    def _newly_disabled(self) -> list[tuple[str, str]]:
        """One error line per API the first time its key is rejected."""
        out = []
        for api, client in (("odds_api", self.odds), ("therundown", self.rundown), ("cfbd", self.cfbd)):
            if client is not None and client.disabled and api not in self._auth_reported:
                self._auth_reported.add(api)
                out.append(("error", f"{api}: invalid key - disabled"))
        return out

    async def _guarded(self, name: str, coro) -> list[tuple[str, str]]:
        try:
            return await coro or []
        except AuthError as e:  # keyed clients disable themselves; disabled ones never call again
            return self._newly_disabled() or [("error", f"{name}: {e}")]
        except Exception as e:
            return [("error", f"{name}: {e}")]

    # -- startup ---------------------------------------------------------
    async def startup(self, now: datetime) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        missing = [api for api, c in (("odds_api", self.odds), ("therundown", self.rundown),
                                      ("cfbd", self.cfbd)) if c is None]
        if missing:
            out.append(("info", f"disabled (no key in .env): {', '.join(missing)}"))
        if not (self.settings.notify_phone or self.settings.ntfy_topic):
            out.append(("info", "notify: no NOTIFY_PHONE or NTFY_TOPIC - texts appear in this log only"))
        out += await self._guarded("teams", self._seed_teams(now))
        out += await self._guarded("games", self._register_games(now))
        out += await self._guarded("nflverse", self._load_nflverse(now))
        out += await self._guarded("nfl model", self._fit_nfl(now))
        out += await self._guarded("cfb model", self._fit_cfb(now))
        out += await self._guarded("gates", self.fit_gates(now))
        for job in (Job("settle"), Job("expire")):
            out += await self.run_job(job, now)
        for name in ("settle", "expire", "cfbd_weekly", "nflverse_refresh"):
            self.scheduler.mark_done(Job(name), now)
        if self._get("first_run_done") is None:
            out += await self._guarded("first-run", self._first_run(now))
            self._put("first_run_done", now.isoformat())
        return out

    async def _seed_teams(self, now: datetime) -> list[tuple[str, str]]:
        if self.cfbd is None:
            return []
        rows = await self.cfbd.teams_fbs(CfbdClient.current_season(now))
        for t in rows:
            school = t.get("school")
            if not school:
                continue
            code = t.get("abbreviation") or school
            names = [school, code, f"{school} {t.get('mascot') or ''}", *(t.get("alternateNames") or [])]
            for n in names:
                if n:
                    self.teams.add("cfb", n, code)
        return [("info", f"teams: {len(rows)} FBS")]

    def _apply_scores(self, games: list[Game]) -> int:
        n = 0
        for g in games:
            if g.status != "unknown":
                self.registry.update_score(g.game_id, g.status, g.home_pts, g.away_pts, g.period, g.clock)
                n += 1
        return n

    async def _register_events(self, league: str) -> int:
        """Register games from the Odds API's free /events listing and remember their event ids."""
        n = 0
        for ev in await self.odds.events(league):
            gid = _register(self.registry, self.teams, league, ev["home_team"], ev["away_team"],
                            datetime.fromisoformat(ev["commence_time"]))
            if gid is not None:
                self.registry.set_odds_event(gid, ev["id"])
                n += 1
        return n

    async def _register_games(self, now: datetime) -> list[tuple[str, str]]:
        from_odds = from_rundown = 0
        if self.odds is not None:
            for league in LEAGUES:
                from_odds += await self._register_events(league)
        if self.rundown is not None:
            today = now.astimezone(PT).date()
            for league in LEAGUES:
                for d in range(8):
                    from_rundown += self._apply_scores(await self.rundown.scores(league, today + timedelta(days=d)))
        return [("info", f"games: {from_odds} from odds events, {from_rundown} from therundown")]

    def _read_nflverse(self, seasons: list[int]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        # schedules ship as one file for every season, so the margin model's 2015+ history is free
        first = min(MARGIN_FIRST_SEASON, seasons[0])
        sched = _dated(self.nflverse.schedules(list(range(first, seasons[-1] + 1))))
        self.history["nfl_margin"] = sched
        return (sched.filter(pl.col("season").is_in(seasons)), self.nflverse.weekly_player_stats(seasons),
                self.nflverse.team_epa(seasons))

    def _read_player_status(self, seasons: list[int]) -> tuple[pl.DataFrame | None, pl.DataFrame | None, list[str]]:
        """nflverse snap counts and injury reports for the projector; either may be unavailable."""
        out: list[pl.DataFrame | None] = []
        missing = []
        for name in ("snap_counts", "injuries"):
            loader = getattr(self.nflverse, name, None)
            try:
                out.append(loader(seasons) if loader else None)
            except Exception:  # noqa: BLE001 - optional input: the projector runs without it
                out.append(None)
            if out[-1] is None:
                missing.append(name.replace("_", " "))
        return out[0], out[1], missing

    def _build_projector(self, weekly: pl.DataFrame, snaps, injuries, cur: int) -> tuple[PropProjector, dict]:
        proj = PropProjector(weekly, snaps, injuries, usage_model=self.settings.props_usage_model)
        return proj, proj.calibration_backtest(cur - 1, self.settings.pp_margin_model)

    async def _load_nflverse(self, now: datetime) -> list[tuple[str, str]]:
        cur = _season(now)
        seasons = list(range(cur - NFL_SEASONS + 1, cur + 1))
        sched, weekly, epa = await asyncio.to_thread(self._read_nflverse, seasons)
        self.history["nfl"], self.history["nfl_epa"] = sched, epa
        self.latest["stats"] = weekly
        out = []
        self.projector_live = False
        if "player_display_name" in weekly.columns:
            snaps = injuries = None
            if self.settings.props_use_snaps_injuries:
                snaps, injuries, missing = await asyncio.to_thread(self._read_player_status, seasons)
                if missing:
                    out.append(("info", f"player-prop model: no {' or '.join(missing)} from nflverse - "
                                        "injury and snap-share filters are off"))
            self.projector, gate = await asyncio.to_thread(self._build_projector, weekly, snaps, injuries, cur)
            self.projector_live = bool(gate["live"])
            if gate["n"]:
                out.append(("info", f"player-prop model check ({cur - 1} season): {gate['n']} picks, said "
                                    f"{gate['claimed']:.0%}, hit {gate['hit']:.0%} - "
                                    + ("model-only prop picks are live" if self.projector_live
                                       else "model-only prop picks are watch-only")))
            else:
                out.append(("info", "player-prop model: not enough history to check it - model-only prop "
                                    "picks are watch-only"))
        else:
            self.projector = _NoProjector()
        self.player_teams = self._latest_teams(weekly)
        weeks = self._assign_nfl_weeks(sched, now)
        return ([("info", f"nflverse: {sched.height} games, {weekly.height} player-weeks, {weeks} weeks set"), *out]
                + await self._check_qbs(sched, cur, now))

    async def _check_qbs(self, sched: pl.DataFrame, season: int, now: datetime) -> list[tuple[str, str]]:
        """Teams whose listed starting QB changed since their last game (their games get no model weight)."""
        self.qb_changes = set()
        if not self.settings.qb_veto:
            return []
        try:
            qb1 = await asyncio.to_thread(self.nflverse.depth_chart_qb1, season)
        except Exception as e:  # noqa: BLE001 - the disagreement cap still guards these games
            return [("info", f"Could not check NFL starting quarterbacks ({type(e).__name__}); "
                             "games where our number is far from the betting market still use the market only")]
        teams = qb_changes(sched, qb1, now.date())
        self.qb_changes = {self.teams.canonical("nfl", t) or t for t in teams}
        if not self.qb_changes:
            return []
        return [("info", f"New starting quarterback since last game: {', '.join(sorted(self.qb_changes))} - "
                         "their games use the betting market's number only")]

    def _latest_teams(self, weekly: pl.DataFrame) -> dict[str, str]:
        """Ruling R2: each player's latest team from the weekly stats."""
        col = next((c for c in ("recent_team", "team") if c in weekly.columns), None)
        if col is None or "player_display_name" not in weekly.columns or weekly.is_empty():
            return {}
        df = (weekly.drop_nulls([col, "player_display_name"]).sort(["season", "week"])
              .group_by("player_display_name").agg(pl.col(col).last()))
        return {name: self.teams.canonical("nfl", t) or t for name, t in df.iter_rows()}

    def _assign_nfl_weeks(self, sched: pl.DataFrame, now: datetime) -> int:
        """Copy nflverse week numbers and neutral sites onto registered NFL games."""
        if sched.is_empty():
            return 0
        slots: dict[tuple[str, str], list[tuple[date, int, int, bool]]] = {}
        for r in sched.drop_nulls(["gameday", "week"]).iter_rows(named=True):
            home = self.teams.canonical("nfl", r["home_team"]) or r["home_team"]
            away = self.teams.canonical("nfl", r["away_team"]) or r["away_team"]
            neutral = is_neutral(r)
            slot = (_as_date(r["gameday"]), int(r["season"]), int(r["week"]), neutral)
            slots.setdefault((home, away), []).append(slot)
            if neutral:  # sources may list a neutral game's teams the other way round
                slots.setdefault((away, home), []).append(slot)
        n = 0
        for g in self.registry.all_since(now - timedelta(days=1)):
            if g.league != "nfl":
                continue
            local = g.kickoff.astimezone(PT).date()
            match = next(((s, w, nt) for d, s, w, nt in slots.get((g.home, g.away), [])
                          if abs((d - local).days) <= 1), None)
            if match and (g.week is None or g.neutral != match[2]):
                self.registry.upsert(replace(g, season=match[0], week=match[1], neutral=match[2]))
                n += 1
        return n

    def _assign_cfb_neutral(self, games: list[dict], now: datetime) -> int:
        """Copy CFBD neutralSite onto registered CFB games (live predictions drop HFA there)."""
        sites = {}
        for r in games:
            d = _as_date(r["startDate"])
            sites[(r["homeTeam"], r["awayTeam"], d)] = sites[(r["awayTeam"], r["homeTeam"], d)] = \
                bool(r.get("neutralSite"))
        n = 0
        for g in self.registry.all_since(now - timedelta(days=1)):
            if g.league != "cfb":
                continue
            day = g.kickoff.astimezone(UTC).date()
            neutral = next((sites[k] for k in ((g.home, g.away, day + timedelta(days=dd)) for dd in (0, -1, 1))
                            if k in sites), None)
            if neutral is not None and neutral != g.neutral:
                self.registry.upsert(replace(g, neutral=neutral))
                n += 1
        return n

    async def _fit_nfl(self, now: datetime) -> list[tuple[str, str]]:
        sched = self.history.get("nfl")
        if sched is None or sched.is_empty():
            return []
        self.nfl_model = await asyncio.to_thread(NflRatings.fit, sched, self.history.get("nfl_epa"), now.date())
        margin = nfl_margin_model(self.history.get("nfl_margin", sched))
        self.margin_models["nfl_spread"] = margin
        games = (f"winning margins learned from {margin.n:,} past games with betting lines"
                 if isinstance(margin, ConditionalMarginModel)
                 else "not enough past games with betting lines yet, so winning margins use a simple bell curve")
        return [("info", f"nfl model: {len(self.nfl_model.ratings)} teams, hfa {self.nfl_model.hfa:+.1f}, {games}")]

    def _cfb_games(self, rows: list[dict]) -> list[dict]:
        def canon(name):
            return self.teams.canonical("cfb", name) or name
        return [{**g, "homeTeam": canon(g["homeTeam"]), "awayTeam": canon(g["awayTeam"])}
                for g in rows if g.get("homeTeam") and g.get("awayTeam") and g.get("startDate")]

    async def _fit_cfb(self, now: datetime) -> list[tuple[str, str]]:
        if self.cfbd is None:
            return []
        year = CfbdClient.current_season(now)
        cur = self._cfb_games(await self.cfbd.games(year))
        if now.month in (12, 1, 2):  # bowls and the CFP (mostly neutral sites): one cached call, in season only
            post = self._cfb_games(await self.cfbd.games(year, season_type="postseason"))
            cur += [g for g in post if g.get("startDate")]
        self._assign_cfb_neutral(cur, now)
        prev = self._cfb_games(await self.cfbd.games(year - 1))
        priors, hfa = await self._cfb_season_prior(year, prev, await self._cfb_market_games(year - 1))
        try:
            model = CfbRatings.fit(cur, priors, now.date())
        except np.linalg.LinAlgError:  # no completed games yet: ratings are the priors alone
            model = CfbRatings(priors, hfa) if priors else None
        self.cfb_model = model
        out = self._fit_cfb_margin(now)
        if model is None:
            return [("info", "cfb model: no games yet"), *out]
        return [("info", f"cfb model: {len(model.ratings)} teams, hfa {model.hfa:+.1f}"), *out]

    def _fit_cfb_margin(self, now: datetime) -> list[tuple[str, str]]:
        """CFB P(margin | spread) from CFBD lines already in the cache (backtests store them).

        Never calls the API: with too little cached history the normal model stays in place.
        """
        spreads, margins = [], []
        if self.cfbd is not None:
            for year in range(MARGIN_FIRST_SEASON, CfbdClient.current_season(now)):
                for r in self.cfbd.cached("/lines", year=year) or []:
                    if r.get("homeScore") is None or r.get("awayScore") is None:
                        continue
                    lines = []
                    for line in r.get("lines") or []:
                        try:
                            lines.append(float(line["spread"]))
                        except (KeyError, TypeError, ValueError):
                            continue
                    if lines:  # CFBD spreads are the home line: negative means home favoured
                        spreads.append(-float(np.median(lines)))
                        margins.append(float(r["homeScore"] - r["awayScore"]))
        if len(spreads) < MIN_EMPIRICAL_RESIDUALS:
            self.margin_models.pop(CFB_MARGIN_KEY, None)
            return [("info", f"college winning margins: only {len(spreads)} saved past games with betting lines, "
                             "so a simple bell curve is used (a college backtest saves more)")]
        self.margin_models[CFB_MARGIN_KEY] = ConditionalMarginModel(spreads, margins, CFB_SIGMA)
        return [("info", f"college winning margins learned from {len(spreads):,} past games with betting lines")]

    # -- gates and backtests ---------------------------------------------
    async def _nfl_sched(self, years: list[int]) -> pl.DataFrame:
        """Schedules for `years` plus one unscored burn-in season, so week 1 has history like live."""
        want = [years[0] - 1, *years]
        sched = self.history.get("nfl")
        if sched is None or not set(want) <= set(sched["season"].unique().to_list()):
            sched = _dated(await asyncio.to_thread(self.nflverse.schedules, want))
        return sched.filter(pl.col("season").is_in(want))

    async def _nfl_inputs(self, years: list[int], before: date):
        sched = await self._nfl_sched(years)
        epa = self.history.get("nfl_epa")

        def predict_asof(d: date):
            model = NflRatings.fit(sched, epa, d)
            return lambda g: model.predict(g["home"], g["away"], g.get("neutral", False))

        games = [g for g in _nfl_history(sched, before) if g["season"] in years]  # burn-in not scored
        return games, predict_asof

    def _cfb_line_rows(self, rows: list[dict], before: date) -> tuple[list[dict], list[dict]]:
        """CFBD /lines rows -> (CfbRatings fit games, walk-forward games with the median closing spread)."""
        fit_games, games = [], []
        for r in rows:
            if r.get("homeScore") is None or r.get("awayScore") is None or not r.get("startDate"):
                continue
            d = _as_date(r["startDate"])
            if d >= before:
                continue
            home = self.teams.canonical("cfb", r["homeTeam"]) or r["homeTeam"]
            away = self.teams.canonical("cfb", r["awayTeam"]) or r["awayTeam"]
            neutral = bool(r.get("neutralSite"))
            fit_games.append({"homeTeam": home, "awayTeam": away, "homePoints": r["homeScore"],
                              "awayPoints": r["awayScore"], "neutralSite": neutral, "week": r.get("week"),
                              "startDate": d})
            spreads = []
            for line in r.get("lines") or []:
                try:
                    spreads.append(float(line["spread"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if spreads:
                games.append({"date": d, "season": r.get("season") or _season_of_date(d), "home": home,
                              "away": away, "neutral": neutral, "home_line": float(np.median(spreads)),
                              "margin": float(r["homeScore"] - r["awayScore"])})
        return fit_games, games

    async def _cfb_market_games(self, year: int) -> list[dict]:
        """A past season's games with closing spreads (one /lines call, cached forever once past)."""
        if self.cfbd is None:
            return []
        return self._cfb_line_rows(await self.cfbd.lines(year, None), date.max)[1]

    async def _cfb_season_prior(self, year: int, prev_games: list[dict],
                                prev_lines: list[dict]) -> tuple[dict[str, float], float]:
        """Season `year`'s CFB prior and HFA, as live: last season's fit, its closing-line
        (market-implied) ratings, and this season's returning production and talent."""
        if not prev_games:
            return {}, 0.0
        canon = lambda n: self.teams.canonical("cfb", n) or n  # noqa: E731
        returning = {canon(r["team"]): float(r["percentPPA"]) for r in await self.cfbd.returning(year)
                     if r.get("team") and r.get("percentPPA") is not None}
        talent = {canon(r.get("team") or r.get("school")): float(r["talent"]) for r in await self.cfbd.talent(year)
                  if (r.get("team") or r.get("school")) and r.get("talent") is not None}
        try:
            last = CfbRatings.fit(prev_games, {}, date(year, 3, 1))
        except np.linalg.LinAlgError:
            return {}, 0.0
        end = max((g["date"] for g in prev_lines), default=None)
        market = implied_ratings([(g["home"], g["away"], g["neutral"], -g["home_line"],
                                   cfb_models.WEEKLY_DECAY ** ((end - g["date"]).days / 7.0))
                                  for g in prev_lines])[0] if prev_lines else None
        return cfb_prior(last.ratings, returning, talent, games_played(prev_games), market), last.hfa

    async def _cfb_inputs(self, years: list[int], before: date):
        """Walk-forward CFB games over `years`, each season fit from its own prior like the live model."""
        if self.cfbd is None:
            return [], None
        by_year = {}
        for y in [years[0] - 1, *years]:  # whole seasons, one call each; past seasons cache forever
            by_year[y] = self._cfb_line_rows(await self.cfbd.lines(y, None), before)
        priors = {y: await self._cfb_season_prior(y, *by_year[y - 1]) for y in years}
        games = [{**g, "season": y} for y in years for g in by_year[y][1]]

        def predict_asof(d: date):
            y = _season_of_date(d)
            prior, hfa = priors.get(y, ({}, 0.0))
            try:
                model = CfbRatings.fit(by_year.get(y, ([], []))[0], prior, d)
            except np.linalg.LinAlgError:  # nothing played this season yet: the prior alone
                if not prior:
                    return lambda g: 0.0
                model = CfbRatings(prior, hfa)
            return lambda g: model.predict(g["home"], g["away"], g["neutral"])

        return games, predict_asof

    async def backtest(self, league: str, start: int, end: int, now: datetime) -> tuple[BlendFit, dict]:
        """Walk-forward ATS over seasons start..end; the model fits run in a thread.

        NFL uses the production spread margin model (empirical once enough residuals exist).
        """
        years = list(range(start, end + 1))
        inputs = self._nfl_inputs if league == "nfl" else self._cfb_inputs
        games, predict = await inputs(years, now.date())
        if not games:
            raise ValueError(f"no {league} history with lines for {start}-{end}")
        mm = self.margin_models.get("nfl_spread") if league == "nfl" else self.margin_models.get("cfb")
        return await asyncio.to_thread(walkforward_ats, league, games, predict, mm)

    async def backtest_totals(self, start: int, end: int, now: datetime) -> tuple[BlendFit, dict]:
        """Walk-forward NFL over/under against the closing total (nflverse total_line)."""
        years = list(range(start, end + 1))
        sched = await self._nfl_sched(years)
        epa = self.history.get("nfl_epa")
        games = []
        for r in sched.drop_nulls(["total_line", "home_score", "away_score", "gameday"]).iter_rows(named=True):
            d = _as_date(r["gameday"])
            if d < now.date() and r["season"] in years:
                games.append({"date": d, "season": r["season"], "home": r["home_team"], "away": r["away_team"],
                              "total_line": float(r["total_line"]),
                              "total": float(r["home_score"] + r["away_score"])})
        if not games:
            raise ValueError(f"no nfl totals history for {start}-{end}")

        def predict_asof(d: date):
            model = NflRatings.fit(sched, epa, d)

            def predict(g):
                t = model.predict_total(g["home"], g["away"])
                return g["total_line"] if t is None else t  # unknown team: no opinion
            return predict

        return await asyncio.to_thread(walkforward_totals, games, predict_asof, self.margin_models.get("nfl_total"))

    @staticmethod
    def _gate_result(fit: BlendFit, metrics: dict, prev: dict | None, recent: bool) -> dict:
        """One market's gate with hysteresis: live only after 2 consecutive passing refits.

        A pass also needs b_model > 0 in every scored season that has its own fit. A failure
        gates at once (the safe direction). A refit within a day of the last one (forced, e.g.
        when a model first appears) does not count as a second pass.
        """
        seasons = metrics.get("by_season") or {}
        passed = not fit.gated and all(v.get("b_model", 1.0) > 0 for v in seasons.values())
        streak = int((prev or {}).get("streak", 0))
        streak = (max(streak, 1) if recent else streak + 1) if passed else 0
        return {**metrics, "gated": not passed or streak < GATE_PASSES, "passed": passed, "streak": streak,
                "b_model": fit.b_model, "se": fit.b_model_se, "n": fit.n, "params": HYPERPARAMS}

    async def fit_gates(self, now: datetime, force: bool = False) -> list[tuple[str, str]]:
        """Auto-gate per league (and NFL totals) from walk-forward backtests; refit at most weekly.

        Fails closed: a league whose backtest errors, or that has a fitted model but no backtest
        history, is gated (observe-only). Only a result with every league backtested is cached.
        """
        stored = json.loads(self._get("gates") or "null")
        spans = self._gate_spans(now)
        if (stored and not force and set(stored["leagues"]) >= set(spans)
                and now - datetime.fromisoformat(stored["fitted_at"]) < GATE_REFIT):
            results = stored["leagues"]
            out = [("info", f"Model checks: using results from {stored['fitted_at'][:10]}")]
            closed: set[str] = set()
        else:
            prev = (stored or {}).get("leagues", {})
            recent = bool(stored) and now - datetime.fromisoformat(stored["fitted_at"]) < GATE_RECENT
            results, out, closed = {}, [], set()
            for league, (start, end) in spans.items():
                try:
                    fit, metrics = await self.backtest(league, start, end, now)
                except ValueError as e:
                    if self._league_model(league) is not None:
                        closed.add(league)
                        _log.info("gate %s: %s", league, e)
                        out.append(("info", f"{_gate_label(league)}: no past games to check the model on yet, "
                                            "so bets are shown but not texted"))
                    else:
                        out.append(("info", f"{_gate_label(league)}: no model or past games yet, nothing to check"))
                    continue
                except Exception as e:  # noqa: BLE001 - one league's failure must not ungate the others
                    closed.add(league)
                    _log.warning("gate %s: backtest failed: %r", league, e)
                    out.append(("error", f"{_gate_label(league)}: could not check the model on past games "
                                         "(data download failed), so bets are shown but not texted"))
                    continue
                results[league] = self._gate_result(fit, metrics, prev.get(league), recent)
                if league == "nfl":
                    try:
                        tfit, tm = await self.backtest_totals(start, end, now)
                        results[league]["total"] = self._gate_result(
                            tfit, tm, prev.get(league, {}).get("total"), recent)
                    except Exception as e:  # noqa: BLE001 - totals stay market-only and gated
                        _log.info("gate nfl totals: %r", e)
                        out.append(("info", "NFL over/unders: could not test our totals model on past games "
                                            "yet, so totals use the sportsbooks' number only"))
            if set(results) >= set(spans):
                self._put("gates", json.dumps({"fitted_at": now.isoformat(), "leagues": results}))
        self.gates_complete = set(results) >= set(spans)
        gates, fits, caps = {}, {}, {}
        for lg, r in results.items():
            for m in ("ml", "spread"):
                gates[(lg, m)] = bool(r["gated"])
                fits[(lg, m)] = (float(r["b_model"]), float(r["se"]))
            tot = r.get("total")
            if lg == "nfl":  # NFL totals have their own model and gate
                gates[(lg, "total")] = True if tot is None else bool(tot["gated"])
                if tot is not None:
                    fits[(lg, "total")] = (float(tot["b_model"]), float(tot["se"]))
            else:
                gates[(lg, "total")] = bool(r["gated"])
            if r.get("disagree_p95") is not None:
                caps[lg] = float(r["disagree_p95"])
        gates.update({(lg, m): True for lg in closed for m in ("ml", "spread", "total")})
        self.gates, self.blend_fits, self.disagree_caps = gates, fits, caps
        for lg, r in results.items():
            out.append(("info", _gate_line(_gate_label(lg), r)))
            if r.get("total") is not None:
                out.append(("info", _gate_line("NFL over/unders", r["total"])))
        return out

    @staticmethod
    def _gate_spans(now: datetime) -> dict[str, tuple[int, int]]:
        cur = _season(now)
        return {"nfl": (cur - NFL_SEASONS + 1, cur), "cfb": (cur - 2, cur - 1)}

    def _league_model(self, league: str):
        return self.nfl_model if league == "nfl" else self.cfb_model

    async def _refit_gates_if_needed(self, league: str, had_model: bool, now: datetime) -> list[tuple[str, str]]:
        """Refit gates when a league's model first appears or the last fit left a league ungraded."""
        if self._league_model(league) is not None and (not had_model or not self.gates_complete):
            return await self._guarded("gates", self.fit_gates(now, force=True))
        return []

    # -- jobs ------------------------------------------------------------
    async def run_job(self, job: Job, now: datetime) -> list[tuple[str, str]]:
        """Run one scheduler job; never raises (errors come back as ("error", ...) lines)."""
        handler = getattr(self, f"_job_{job.name}", None)
        if handler is None:
            return [("error", f"{job.name}: unknown job")]
        return await self._guarded(job.name, handler(job, now))

    def _merge(self, key: str, source: str, items: list, now: datetime) -> None:
        """Replace one source's items for the games it just returned; drop anything stale; persist."""
        ids = {i.game_id for i in items}
        if key == "props":  # prop ages run from when we pulled them, not the book's own stamp
            items = [i if i.fetched_at else replace(i, fetched_at=now) for i in items]
        keep = [q for q in self.latest[key] if not (q.source == source and q.game_id in ids)
                and now - _seen_at(q) < QUOTE_TTL]
        self.latest[key] = keep + items
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, fetched_at, immutable, body) VALUES (?,?,0,?)",
            (f"latest:{key}", now.astimezone(UTC).isoformat(), _dump_items(self.latest[key])))

    def load_latest(self, now: datetime) -> None:
        """Reload persisted quotes and prop lines (the scheduler won't re-pull them after a restart)."""
        for key, cls in _PERSISTED.items():
            row = self.conn.execute("SELECT body FROM api_cache WHERE key=?", (f"latest:{key}",)).fetchone()
            if row is None:
                continue
            try:
                items = _load_items(cls, row["body"])
            except (TypeError, ValueError):
                continue
            self.latest[key] = [i for i in items if now - _seen_at(i) < QUOTE_TTL]

    # -- open positions ----------------------------------------------------
    def open_bet_games(self) -> set[str]:
        return {leg["game_id"] for leg in _bet_legs(self.lifecycle.open_bets()) if leg.get("game_id")}

    def close_games(self) -> set[str]:
        """Games whose closing line matters: a PLACED bet, or an active non-observe recommendation."""
        rows = self.conn.execute("SELECT legs_json FROM recommendations "
                                 "WHERE state IN ('RECOMMENDED','PENDING') AND observe_only=0").fetchall()
        return self.open_bet_games() | {leg["game_id"] for leg in _bet_legs(rows) if leg.get("game_id")}

    async def _job_expire(self, job: Job, now: datetime):
        ids = self.lifecycle.expire_due(now)
        return [("info", f"expired {len(ids)} recommendation(s)")] if ids else []

    async def _job_rundown_scores(self, job: Job, now: datetime):
        if self.rundown is None:
            return []
        n = 0
        essential = any(g.startswith(f"{job.league}:") for g in self.open_bet_games())
        for day in sorted({(now - timedelta(hours=12)).date(), now.date()}):
            n += self._apply_scores(await self.rundown.scores(job.league, day, essential=essential))
        if self.rundown.last_status == "skipped_budget":
            return [("info", f"therundown scores {job.league}: skipped to protect the daily/monthly points")]
        return [("api", f"therundown scores {job.league}: {n} games")]

    async def _job_rundown_lines(self, job: Job, now: datetime):
        if self.rundown is None:
            return []
        # only the dates of games kicking off within 48 h - usually one call, not two
        days = sorted({g.kickoff.astimezone(UTC).date()
                       for g in self.registry.upcoming(LINES_HORIZON, now) if g.league == job.league})
        quotes = []
        for day in days:
            quotes += await self.rundown.lines(job.league, day, fetched_at=now)
        if self.rundown.last_status == "skipped_budget":
            return [("info", f"therundown lines {job.league}: skipped to protect the daily/monthly points")]
        self._merge("quotes", "therundown", quotes, now)
        return [("api", f"therundown lines {job.league}: {len(quotes)} quotes")]

    async def _job_odds_games(self, job: Job, now: datetime):
        if self.odds is None:
            return []
        await self._register_events(job.league)  # free, so new games resolve before the paid pull
        quotes = await self.odds.game_odds(job.league, job.args.get("commence_from"),
                                           job.args.get("commence_to"), essential=job.essential,
                                           fetched_at=now)
        if self.odds.last_status == "skipped_budget":
            return [("info", f"odds_api games {job.league}: skipped to protect the credit reserve")]
        self._merge("quotes", "odds_api", quotes, now)
        out = [("api", f"odds_api games {job.league} ({job.args.get('window', 'manual')}): {len(quotes)} quotes, "
                       f"{self.odds.budget.remaining()} credits left")]
        if str(job.args.get("window", "")).startswith("close:"):
            out += self._record_clv(now)
        return out

    async def _job_odds_props(self, job: Job, now: datetime):
        if self.odds is None:
            return []
        gid = job.args["game_id"]
        row = self.conn.execute("SELECT odds_event_id FROM games WHERE game_id=?", (gid,)).fetchone()
        if row is None or row["odds_event_id"] is None:
            return [("info", f"odds_api props: no odds event for {gid}")]
        props = await self.odds.event_props(job.league, row["odds_event_id"], essential=job.essential)
        self._merge("props", "odds_api", props, now)
        return [("api", f"odds_api props {gid}: {len(props)} lines, {self.odds.budget.remaining()} credits left")]

    async def _job_kalshi_refresh(self, job: Job, now: datetime):
        series = [s for lg in GAME_SERIES.values() for s in lg.values()] + list(PROP_SERIES_STAT)
        markets: list[KalshiMarket] = []
        try:
            for s in series:
                await self.kalshi.fee_multiplier(s)
                markets += await self.kalshi.markets(s)
        except Exception:
            self.kalshi_ok = False
            raise
        self.kalshi_ok = True
        self.latest["kalshi"] = markets
        unmatched = sum(m.game_id is None for m in markets)
        return [("api", f"kalshi: {len(markets)} markets ({unmatched} unmatched)")]

    async def _job_cfbd_weekly(self, job: Job, now: datetime):
        had = self.cfb_model is not None
        out = await self._fit_cfb(now)
        return out + await self._refit_gates_if_needed("cfb", had, now)

    async def _job_nflverse_refresh(self, job: Job, now: datetime):
        had = self.nfl_model is not None
        out = await self._load_nflverse(now) + await self._fit_nfl(now)
        return out + await self._refit_gates_if_needed("nfl", had, now)

    async def _job_settle(self, job: Job, now: datetime):
        open_bets = self.lifecycle.open_bets()
        if not open_bets:
            return []
        markets = {m.ticker: m for m in self.latest["kalshi"]}
        for bet in open_bets:
            for leg in json.loads(bet["legs_json"]):
                t, g = leg.get("ticker"), self.registry.get(leg.get("game_id") or "")
                if t and t not in markets and g is not None and g.kickoff <= now:
                    try:  # settled markets drop out of the open-market refresh
                        markets[t] = await self.kalshi.market(t)
                    except ApiError:
                        continue
        done = self.settler.settle_ready(now, markets.get, self._stat_lookup)
        return [("settle", f"bet {i} {result}: ${payout / 100:,.2f}") for i, result, payout in done]

    def _stat_lookup(self, game_id: str, player: str, stat: str):
        """A player's stat from nflverse weekly stats: value, None (did not play) or "pending"."""
        g = self.registry.get(game_id)
        df, col = self.latest["stats"], STAT_COLUMNS.get(stat)
        if (g is None or g.status != "final" or g.league != "nfl" or g.week is None or df is None
                or col is None or col not in df.columns or "player_display_name" not in df.columns):
            return "pending"
        week = df.filter((pl.col("season") == g.season) & (pl.col("week") == g.week))
        team_col = next((c for c in ("recent_team", "team") if c in df.columns), None)
        if team_col is not None:
            week = week.filter(pl.col(team_col).is_in([g.home, g.away]))
        if week.is_empty():  # this game's stats are not published yet
            return "pending"
        key = normalize_name(player)
        row = week.filter(pl.col("player_display_name").map_elements(normalize_name, return_dtype=pl.Utf8) == key)
        if row.is_empty() or row[col][0] is None:
            return None
        return float(row[col][0])

    def _record_clv(self, now: datetime) -> list[tuple[str, str]]:
        """CLV for open single-leg Kalshi game bets kicking off soon, from the closing market fair."""
        markets = {m.ticker: m for m in self.latest["kalshi"]}
        snap = self.snapshot(now)
        out = []
        for bet in self.lifecycle.open_bets():
            if bet["kind"] != "kalshi_game":
                continue
            leg = Leg(**json.loads(bet["legs_json"])[0])
            g, m = self.registry.get(leg.game_id or ""), markets.get(leg.ticker or "")
            if g is None or m is None or not now <= g.kickoff <= now + timedelta(minutes=30):
                continue
            p_yes = pipeline.market_p_yes(m, g, snap, self.settings)  # devigged close, no model, any league
            if p_yes is None:
                continue
            value = self.settler.record_clv(bet["id"], p_yes if leg.side == "yes" else 1 - p_yes)
            if value is not None:
                out.append(("info", f"clv bet {bet['id']}: {value:+.1%}"))
        return out

    # -- scanning --------------------------------------------------------
    def _predictions(self, games: dict[str, Game]) -> dict[str, Prediction]:
        out = {}
        for g in games.values():
            if g.league == "nfl" and self.nfl_model and {g.home, g.away} <= self.nfl_model.ratings.keys():
                qb = sorted({g.home, g.away} & self.qb_changes) if self.settings.qb_veto else []
                out[g.game_id] = Prediction(g.game_id, self.nfl_model.predict(g.home, g.away, g.neutral),
                                            self.nfl_model.predict_total(g.home, g.away), nfl_models.MODEL_VERSION,
                                            veto=f"QB change: {', '.join(qb)}" if qb else None)
            elif g.league == "cfb" and self.cfb_model and {g.home, g.away} <= self.cfb_model.ratings.keys():
                out[g.game_id] = Prediction(g.game_id, self.cfb_model.predict(g.home, g.away, g.neutral),
                                            None, cfb_models.MODEL_VERSION)
        return out

    def _clv_mult(self) -> dict[str, float]:
        """Kelly multiplier per league from the CLV of its latest game bets (recorded at kickoff)."""
        per: dict[str, list[float]] = {}
        window = self.settings.clv_throttle_window
        for r in self.conn.execute("SELECT legs_json, clv FROM bets WHERE kind='kalshi_game' AND clv IS NOT NULL "
                                   "ORDER BY placed_at DESC, id DESC"):
            gid = (json.loads(r["legs_json"])[0].get("game_id") or "")
            g = self.registry.get(gid)
            league = g.league if g is not None else gid.split(":")[0]
            vals = per.setdefault(league, [])
            if len(vals) < window:
                vals.append(r["clv"])
        return {lg: clv_throttle(v, self.settings.clv_throttle_min) for lg, v in per.items()}

    def snapshot(self, now: datetime) -> pipeline.Snapshot:
        games = {g.game_id: g for g in self.registry.all_since(now - timedelta(days=1))}
        exposure = self.lifecycle.open_exposure_cents()
        bets = self.lifecycle.open_bets()
        game_legs = _bet_legs([b for b in bets if b["kind"] == "kalshi_game"])
        prop_legs = _bet_legs([b for b in bets if b["kind"] == "kalshi_prop"])
        pp_legs = _bet_legs([b for b in bets if b["kind"] == "pp_entry"])
        pp_players = {normalize_name(leg["player"]) for leg in pp_legs if leg.get("player")}
        positions = self.lifecycle.open_positions()
        exp = pipeline.open_exposure(positions, {**games, **{
            gid: g for _, _, legs in positions for leg in legs
            if (gid := leg.game_id) and gid not in games and (g := self.registry.get(gid)) is not None}})
        clv_mult = self._clv_mult()
        gates = dict(self.gates)
        for league, mult in clv_mult.items():
            if mult <= 0:  # closing-line value significantly negative: observe only until it recovers
                gates.update({(league, m): True for m in ("ml", "spread", "total")})
        open_keys = {r["leg_key"] for r in self.conn.execute(
            "SELECT r.leg_key FROM recommendations r JOIN bets b ON b.rec_id = r.id "
            "WHERE b.state='PLACED' AND r.state='PLACED'")}
        return pipeline.Snapshot(
            now=now, games=games, quotes=list(self.latest["quotes"]),
            props=self.latest["props"] + self.manual_props, kalshi=list(self.latest["kalshi"]),
            predictions=self._predictions(games), gates=gates,
            # the ledger already holds open stakes out, so the Kelly bankroll adds them back
            bankroll_cents=self.ledger.balance() + exposure, open_exposure_cents=exposure,
            pp_staked_today_cents=self.lifecycle.pp_staked_today_cents(now, PT),
            margin_models=self.margin_models, player_teams={**self.player_teams, **self.manual_teams},
            open_game_ids={leg["game_id"] for leg in game_legs if leg.get("game_id")},
            # no Kalshi prop on a player already held on Kalshi or in an open PrizePicks entry
            open_players={normalize_name(leg["player"]) for leg in prop_legs if leg.get("player")} | pp_players,
            open_leg_keys=open_keys, blend_fits=dict(self.blend_fits), disagree_caps=dict(self.disagree_caps),
            projector_live=self.projector_live,
            open_game_exposure=exp.game, open_window_exposure=exp.window, open_dir_exposure=exp.direction,
            placed_today_cents=self.lifecycle.placed_today_cents(now, PT),
            pp_open_players=pp_players,
            pp_open_games={leg["game_id"] for leg in pp_legs if leg.get("game_id")},
            clv_mult=clv_mult)

    def _wanted(self, rec: Recommendation, what: str) -> bool:
        venue = "prizepicks" if rec.kind == "pp_entry" else "kalshi"
        return venue in self.settings.venues and (what == "all" or rec.kind in SCAN_KINDS[what])

    async def _job_scan(self, job: Job, now: datetime):
        what = job.args.get("what", "all")
        recs = [r for r in pipeline.scan(self.snapshot(now), self.settings, self.projector, _league_of)
                if self._wanted(r, what)]
        out = []
        for rec in recs:
            saved, new = self.lifecycle.upsert_recommendation(rec, now)
            if not new:
                continue
            out.append(("rec", _describe(saved)))
            if saved.observe_only:
                continue
            res = self.notifier.notify(saved, now)
            if res is None:
                continue  # notifications off, quiet hours, or already sent
            if res.ok:
                self.lifecycle.mark_pending(saved.id)
                sent = f"#{saved.id} sent via {res.channel}"
                if res.channel == ConsoleNotifier.name:  # no phone channel worked: show the text here
                    sent += f": {format_message(saved, PT)}"
                out.append(("text", sent))
            else:
                out.append(("error", f"text for #{saved.id} failed: {res.error}"))
        new_names = sorted(set(getattr(self.projector, "unmatched", ())) - self._reported_unmatched)
        if new_names:
            self._reported_unmatched |= set(new_names)
            out.append(("info", "player-prop model: no stats found for " + ", ".join(new_names[:10])
                        + (f" and {len(new_names) - 10} more" if len(new_names) > 10 else "")))
        if not out:
            out.append(("info", f"scan {what}: {len(recs)} opportunities, nothing new"))
        return out

    # -- commands ---------------------------------------------------------
    def add_manual_pp(self, player: str, stat: str, line: float, team: str, game_id: str, now: datetime) -> None:
        """`pp add`: a PrizePicks line typed in by hand (used when the Odds API key lacks PrizePicks)."""
        g = self.registry.get(game_id)
        if g is None:
            raise ValueError(f"unknown game {game_id}")
        self.manual_props = [q for q in self.manual_props
                             if not (q.game_id == game_id and q.player == player and q.stat == stat)]
        self.manual_props.append(PropQuote("manual", "prizepicks", game_id, player, stat, "over", line, None, now))
        self.manual_teams[player] = self.teams.canonical(g.league, team) or team
        self.scheduler.request_scan(now)

    def notify_test(self, now: datetime):
        return self.notifier.send_text(NOTIFY_TEST_TEXT, now)

    def quota_lines(self) -> list[str]:
        out = []
        for api, guard in self.budgets.items():
            rem = guard.remaining()
            out.append(f"{api}: {'unknown' if rem is None else rem} of {guard.monthly_cap} left")
        out.append(f"kalshi: {'ok' if self.kalshi_ok else 'not reached yet'}")
        return out

    async def _first_run(self, now: datetime) -> list[tuple[str, str]]:
        out = []
        if self.odds is not None and not self.odds.disabled:
            if not self.odds.seen_books:  # one game-odds pull and one props probe reveal the key's books
                self._merge("quotes", "odds_api", await self.odds.game_odds("nfl", fetched_at=now), now)
                row = self.conn.execute(
                    "SELECT game_id, odds_event_id FROM games WHERE league='nfl' AND odds_event_id IS NOT NULL"
                    " AND kickoff_utc > ? ORDER BY kickoff_utc LIMIT 1", (now.astimezone(UTC).isoformat(),)
                ).fetchone()
                if row is not None:
                    self._merge("props", "odds_api", await self.odds.event_props("nfl", row["odds_event_id"]), now)
            books = sorted(self.odds.seen_books)
            out.append(("info", f"odds_api books: {', '.join(books) or 'none'}"))
            if "prizepicks" not in books:
                out.append(("info", 'PrizePicks lines unavailable on this key - use "pp add"'))
        if self.rundown is not None:
            rd = self.rundown
            out.append(("info", f"therundown tier={rd.tier} delay={rd.delay_seconds}s books={rd.bookmakers_header}"))
        if self.cfbd is not None and not self.cfbd.disabled:
            info = await self.cfbd.info()
            out.append(("info", f"cfbd remaining={info.get('remainingCalls')}"))
        try:
            await self.kalshi.markets(GAME_SERIES["nfl"]["ml"])
            self.kalshi_ok = True
        except Exception:
            self.kalshi_ok = False
        out.append(("info", f"kalshi reachable={self.kalshi_ok}"))
        out.append(("info", 'notify: run "notify test" to confirm your phone alerts'))
        if "prizepicks" in self.settings.venues and not self.settings.pp_payouts_verified:
            out.append(("info", "PrizePicks payouts not confirmed: check your app pays 6x for a 3-pick Power "
                                "and 10x / 2x / 0.4x for a 5-pick Flex, then type: set pp_payouts_verified true "
                                "(PrizePicks stakes are halved until then). If only a perfect card pays (Arena), "
                                "type: set pp_mode arena"))
        return out

    # -- views -------------------------------------------------------------
    def kpis(self) -> dict:
        rows = self.conn.execute(
            "SELECT b.*, r.p_win FROM bets b LEFT JOIN recommendations r ON r.id = b.rec_id "
            "WHERE b.state != 'PLACED'").fetchall()
        return compute_kpis(rows, self.ledger.history())

    def balance_markers(self) -> list[tuple[datetime, int, str]]:
        """(time, balance after, kind) for each deposit and withdrawal, for the chart."""
        out, total = [], 0
        for r in self.conn.execute("SELECT ts, kind, amount_cents FROM ledger ORDER BY id"):
            total += r["amount_cents"]
            if r["kind"] in ("deposit", "withdraw"):
                out.append((datetime.fromisoformat(r["ts"]), total, r["kind"]))
        return out

    async def aclose(self) -> None:
        for client in (self.odds, self.kalshi, self.rundown, self.cfbd):
            if client is not None:
                await client.aclose()


def _stored_overrides(conn: sqlite3.Connection, settings: Settings) -> Settings:
    """Re-apply earlier `set` commands saved in the settings table; skip any that no longer validate."""
    for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'set.%'"):
        name = r["key"].removeprefix("set.")
        if name in LOCKED_SETTINGS:
            continue
        try:
            settings = apply_override(settings, name, r["value"])
        except ValueError:
            continue
    return settings


def build_services(settings: Settings, transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> Services:
    """Everything the app needs. A client whose key is missing is None and its jobs are skipped."""
    transports = transports or {}
    conn = connect(settings.db_path)
    settings = _stored_overrides(conn, settings)
    ledger = Ledger(conn)
    lifecycle = Lifecycle(conn, ledger)
    teams = TeamResolver(conn)
    registry = GameRegistry(conn, teams)

    def resolve_or_register(league, home_name, away_name, kickoff):
        return _register(registry, teams, league, home_name, away_name, kickoff)

    odds = rundown = cfbd = None
    if settings.odds_api_key:
        odds = OddsApiClient(settings.odds_api_key, conn, registry.resolve,
                             client=_http(transports, "odds", odds_api.BASE_URL, rate=5.0))
    if settings.therundown_api_key:
        rundown = TheRundownClient(
            settings.therundown_api_key, conn, resolve_or_register,
            client=_http(transports, "rundown", therundown.BASE_URL,
                         {"X-TheRundown-Key": settings.therundown_api_key}, rate=1 / 1.1))
    if settings.cfbd_api_key:
        cfbd = CfbdClient(settings.cfbd_api_key, conn,
                          client=_http(transports, "cfbd", CFBD_URL,
                                       {"Authorization": f"Bearer {settings.cfbd_api_key}"}))
    kalshi = KalshiClient(settings.kalshi_base_url, kalshi_resolver(registry, teams),
                          client=_http(transports, "kalshi", settings.kalshi_base_url))
    budgets = {api: c.budget for api, c in (("odds_api", odds), ("cfbd", cfbd), ("therundown", rundown))
               if c is not None}

    sink: list[str] = []
    channels: list = []
    if settings.notify_phone:
        channels.append(IMessageNotifier(settings.notify_phone))
    if settings.ntfy_topic:
        channels.append(NtfyNotifier(settings.ntfy_topic))
    channels.append(ConsoleNotifier(sink))  # last resort: the text shows in the event log
    notifier = NotifyChain(conn, channels, settings.quiet_hours, settings.notify_enabled)

    view = _RegistryView(registry, lambda: datetime.now(UTC))
    scheduler = Scheduler(view, budgets, state=_SchedulerState(conn, datetime.now(UTC)),
                          has_open_bets=lambda: bool(lifecycle.open_bets()),
                          open_bet_games=lambda: services.open_bet_games(),
                          close_games=lambda: services.close_games())
    services = Services(
        settings=settings, conn=conn, ledger=ledger, lifecycle=lifecycle,
        settler=Settler(conn, lifecycle, registry, payout_table(settings)), teams=teams, registry=registry, budgets=budgets,
        scheduler=scheduler, notifier=notifier, odds=odds, kalshi=kalshi, rundown=rundown, cfbd=cfbd,
        nflverse=NflverseSource(settings.db_path.parent / "nflverse"), console_sink=sink)
    view.clock = lambda: services.clock()
    services.load_latest(datetime.now(UTC))
    return services
