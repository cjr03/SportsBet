"""Wires settings, db, clients, models, notifier and scheduler together; runs startup and fetch jobs."""
import asyncio
import json
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
from gridiron_edge.matching import GameRegistry, TeamResolver, kalshi_resolver
from gridiron_edge.models import Prediction
from gridiron_edge.models import cfb as cfb_models
from gridiron_edge.models import nfl as nfl_models
from gridiron_edge.models.blend import BlendFit, walkforward_ats
from gridiron_edge.models.cfb import CfbRatings, cfb_prior
from gridiron_edge.models.nfl import NflRatings
from gridiron_edge.models.props import STAT_COLUMNS, PropProjector
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
from gridiron_edge.types import Game, KalshiMarket, Leg, PropQuote, Quote, Recommendation, make_game_id
from gridiron_edge.ui.widgets import compute_kpis

register_schema("CREATE TABLE IF NOT EXISTS scheduler_state (key TEXT PRIMARY KEY, ts TEXT NOT NULL)")

STATE_TTL = timedelta(days=14)
UPCOMING_WINDOW = timedelta(days=8)
GATE_REFIT = timedelta(days=7)
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
SCAN_KINDS = {"games": {"kalshi_game"}, "props": {"kalshi_prop"}, "pp": {"pp_entry"}, "arb": {"arb"}}
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
                        "home_line": -float(r["spread_line"]), "margin": float(r["result"])})
    return out


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

    async def _load_nflverse(self, now: datetime) -> list[tuple[str, str]]:
        cur = _season(now)
        sched, weekly, epa = await asyncio.to_thread(self._read_nflverse, list(range(cur - NFL_SEASONS + 1, cur + 1)))
        self.history["nfl"], self.history["nfl_epa"] = sched, epa
        self.latest["stats"] = weekly
        self.projector = PropProjector(weekly) if "player_display_name" in weekly.columns else _NoProjector()
        self.player_teams = self._latest_teams(weekly)
        weeks = self._assign_nfl_weeks(sched, now)
        return [("info", f"nflverse: {sched.height} games, {weekly.height} player-weeks, {weeks} weeks set")]

    def _latest_teams(self, weekly: pl.DataFrame) -> dict[str, str]:
        """Ruling R2: each player's latest team from the weekly stats."""
        col = next((c for c in ("recent_team", "team") if c in weekly.columns), None)
        if col is None or "player_display_name" not in weekly.columns or weekly.is_empty():
            return {}
        df = (weekly.drop_nulls([col, "player_display_name"]).sort(["season", "week"])
              .group_by("player_display_name").agg(pl.col(col).last()))
        return {name: self.teams.canonical("nfl", t) or t for name, t in df.iter_rows()}

    def _assign_nfl_weeks(self, sched: pl.DataFrame, now: datetime) -> int:
        """Copy nflverse week numbers onto registered NFL games (props projection needs them)."""
        if sched.is_empty():
            return 0
        slots: dict[tuple[str, str], list[tuple[date, int, int]]] = {}
        for r in sched.drop_nulls(["gameday", "week"]).iter_rows(named=True):
            home = self.teams.canonical("nfl", r["home_team"]) or r["home_team"]
            away = self.teams.canonical("nfl", r["away_team"]) or r["away_team"]
            slots.setdefault((home, away), []).append((_as_date(r["gameday"]), int(r["season"]), int(r["week"])))
        n = 0
        for g in self.registry.all_since(now - timedelta(days=1)):
            if g.league != "nfl" or g.week is not None:
                continue
            local = g.kickoff.astimezone(PT).date()
            match = next(((s, w) for d, s, w in slots.get((g.home, g.away), [])
                          if abs((d - local).days) <= 1), None)
            if match:
                self.registry.upsert(replace(g, season=match[0], week=match[1]))
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
        prev = self._cfb_games(await self.cfbd.games(year - 1))
        canon = lambda n: self.teams.canonical("cfb", n) or n  # noqa: E731
        returning = {canon(r["team"]): float(r["percentPPA"]) for r in await self.cfbd.returning(year)
                     if r.get("team") and r.get("percentPPA") is not None}
        talent = {canon(r.get("team") or r.get("school")): float(r["talent"]) for r in await self.cfbd.talent(year)
                  if (r.get("team") or r.get("school")) and r.get("talent") is not None}
        priors: dict[str, float] = {}
        hfa = 0.0
        try:
            if prev:
                last = CfbRatings.fit(prev, {}, date(year, 3, 1))
                priors, hfa = cfb_prior(last.ratings, returning, talent), last.hfa
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
    async def _nfl_inputs(self, years: list[int], before: date):
        sched = self.history.get("nfl")
        if sched is None or not set(years) <= set(sched["season"].unique().to_list()):
            sched = _dated(await asyncio.to_thread(self.nflverse.schedules, years))
        sched = sched.filter(pl.col("season").is_in(years))
        epa = self.history.get("nfl_epa")

        def predict_asof(d: date):
            model = NflRatings.fit(sched, epa, d)
            return lambda g: model.predict(g["home"], g["away"])

        return _nfl_history(sched, before), predict_asof

    async def _cfb_inputs(self, years: list[int], before: date):
        if self.cfbd is None:
            return [], None
        rows = []
        for y in years:
            rows += await self.cfbd.lines(y, None)  # whole season in one call; past seasons cache forever
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
                games.append({"date": d, "home": home, "away": away, "neutral": neutral,
                              "home_line": float(np.median(spreads)),
                              "margin": float(r["homeScore"] - r["awayScore"])})

        def predict_asof(d: date):
            try:
                model = CfbRatings.fit(fit_games, {}, d)
            except np.linalg.LinAlgError:  # nothing played before d yet
                return lambda g: 0.0
            return lambda g: model.predict(g["home"], g["away"], g["neutral"])

        return games, predict_asof

    async def backtest(self, league: str, start: int, end: int, now: datetime) -> tuple[BlendFit, dict]:
        """Walk-forward ATS over seasons start..end; the model fits run in a thread."""
        years = list(range(start, end + 1))
        inputs = self._nfl_inputs if league == "nfl" else self._cfb_inputs
        games, predict = await inputs(years, now.date())
        if not games:
            raise ValueError(f"no {league} history with lines for {start}-{end}")
        return await asyncio.to_thread(walkforward_ats, league, games, predict)

    async def fit_gates(self, now: datetime, force: bool = False) -> list[tuple[str, str]]:
        """Auto-gate per league from a walk-forward backtest; refit at most weekly unless forced.

        Fails closed: a league whose backtest errors, or that has a fitted model but no backtest
        history, is gated (observe-only). Only a result with every league backtested is cached.
        """
        stored = json.loads(self._get("gates") or "null")
        spans = self._gate_spans(now)
        if (stored and not force and set(stored["leagues"]) >= set(spans)
                and now - datetime.fromisoformat(stored["fitted_at"]) < GATE_REFIT):
            results = stored["leagues"]
            out = [("info", f"gates: cached from {stored['fitted_at'][:10]}")]
            closed: set[str] = set()
        else:
            results, out, closed = {}, [], set()
            for league, (start, end) in spans.items():
                try:
                    fit, metrics = await self.backtest(league, start, end, now)
                except ValueError as e:
                    if self._league_model(league) is not None:
                        closed.add(league)
                        out.append(("info", f"gate {league}: {e} - observe-only until a backtest runs"))
                    else:
                        out.append(("info", f"gate {league}: no backtest history - not gated"))
                    continue
                except Exception as e:  # noqa: BLE001 - one league's failure must not ungate the others
                    closed.add(league)
                    out.append(("error", f"gate {league}: backtest failed ({e}) - observe-only"))
                    continue
                results[league] = {"gated": fit.gated, "b_model": fit.b_model, "se": fit.b_model_se, **metrics}
            if set(results) >= set(spans):
                self._put("gates", json.dumps({"fitted_at": now.isoformat(), "leagues": results}))
        self.gates_complete = set(results) >= set(spans)
        gates = {(lg, m): bool(r["gated"]) for lg, r in results.items() for m in ("ml", "spread", "total")}
        gates.update({(lg, m): True for lg in closed for m in ("ml", "spread", "total")})
        self.gates = gates
        for lg, r in results.items():
            out.append(("info", f"gate {lg}: n={r['n']} b_model={r['b_model']:.2f}±{r['se']:.2f} "
                                f"-> {'observe-only' if r['gated'] else 'live'}"))
        return out

    @staticmethod
    def _gate_spans(now: datetime) -> dict[str, tuple[int, int]]:
        cur = _season(now)
        return {"nfl": (cur - NFL_SEASONS + 1, cur), "cfb": (cur - 1, cur - 1)}

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
        row = week.filter(pl.col("player_display_name").str.to_lowercase() == player.lower())
        if row.is_empty() or row[col][0] is None:
            return None
        return float(row[col][0])

    def _record_clv(self, now: datetime) -> list[tuple[str, str]]:
        """CLV for open single-leg Kalshi game bets kicking off soon, from the closing market fair."""
        markets = {m.ticker: m for m in self.latest["kalshi"]}
        snap = replace(self.snapshot(now), predictions={})
        out = []
        for bet in self.lifecycle.open_bets():
            if bet["kind"] != "kalshi_game":
                continue
            leg = Leg(**json.loads(bet["legs_json"])[0])
            g, m = self.registry.get(leg.game_id or ""), markets.get(leg.ticker or "")
            if g is None or m is None or not now <= g.kickoff <= now + timedelta(minutes=30):
                continue
            p_yes = pipeline._game_p_yes(m, g, snap, self.settings)
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
                out[g.game_id] = Prediction(g.game_id, self.nfl_model.predict(g.home, g.away, g.neutral),
                                            self.nfl_model.predict_total(g.home, g.away), nfl_models.MODEL_VERSION)
            elif g.league == "cfb" and self.cfb_model and {g.home, g.away} <= self.cfb_model.ratings.keys():
                out[g.game_id] = Prediction(g.game_id, self.cfb_model.predict(g.home, g.away, g.neutral),
                                            None, cfb_models.MODEL_VERSION)
        return out

    def snapshot(self, now: datetime) -> pipeline.Snapshot:
        games = {g.game_id: g for g in self.registry.all_since(now - timedelta(days=1))}
        exposure = self.lifecycle.open_exposure_cents()
        bets = self.lifecycle.open_bets()
        game_legs = _bet_legs([b for b in bets if b["kind"] == "kalshi_game"])
        prop_legs = _bet_legs([b for b in bets if b["kind"] == "kalshi_prop"])
        open_keys = {r["leg_key"] for r in self.conn.execute(
            "SELECT r.leg_key FROM recommendations r JOIN bets b ON b.rec_id = r.id "
            "WHERE b.state='PLACED' AND r.state='PLACED'")}
        return pipeline.Snapshot(
            now=now, games=games, quotes=list(self.latest["quotes"]),
            props=self.latest["props"] + self.manual_props, kalshi=list(self.latest["kalshi"]),
            predictions=self._predictions(games), gates=dict(self.gates),
            # the ledger already holds open stakes out, so the Kelly bankroll adds them back
            bankroll_cents=self.ledger.balance() + exposure, open_exposure_cents=exposure,
            pp_staked_today_cents=self.lifecycle.pp_staked_today_cents(now, PT),
            margin_models=self.margin_models, player_teams={**self.player_teams, **self.manual_teams},
            open_game_ids={leg["game_id"] for leg in game_legs if leg.get("game_id")},
            open_players={leg["player"].lower() for leg in prop_legs if leg.get("player")},
            open_leg_keys=open_keys)

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
        settler=Settler(conn, lifecycle, registry), teams=teams, registry=registry, budgets=budgets,
        scheduler=scheduler, notifier=notifier, odds=odds, kalshi=kalshi, rundown=rundown, cfbd=cfbd,
        nflverse=NflverseSource(settings.db_path.parent / "nflverse"), console_sink=sink)
    view.clock = lambda: services.clock()
    services.load_latest(datetime.now(UTC))
    return services
