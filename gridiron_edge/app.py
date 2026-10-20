"""The Textual app: dashboard layout, command bar, and scheduler ticks run as workers."""
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input

from gridiron_edge.bets.lifecycle import LifecycleError
from gridiron_edge.bets.settle import _yes_from_score
from gridiron_edge.db import InsufficientFunds, InvalidAmount
from gridiron_edge.notify.format import PT
from gridiron_edge.scheduler import LEAGUES, Job
from gridiron_edge.services import NOTIFY_TEST_TEXT, Services
from gridiron_edge.types import Leg, Recommendation
from gridiron_edge.ui.commands import COMMAND_WORDS, Command, CommandError, parse_command
from gridiron_edge.ui.screens import ConfirmScreen
from gridiron_edge.ui.widgets import (
    BalanceChart, BankrollHeader, EventLog, KpiTiles, OpportunityBoard, QuotaBar, ScoresPanel,
)

ODDS_GAMES_COST = 3  # credits per league for h2h+spreads+totals
_USER_ERRORS = (CommandError, LifecycleError, InvalidAmount, ValueError)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _funds_error(e: InsufficientFunds) -> str:
    msg = str(e)
    return msg if "insufficient" in msg.lower() else f"insufficient funds: {msg}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1%}"


class GridironApp(App):
    CSS_PATH = "ui/theme.tcss"
    AUTO_FOCUS = "#cmd"
    TITLE = "Gridiron Edge"
    TICK_S = 15

    def __init__(self, services: Services, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        super().__init__()
        self.services = services
        self.clock = clock
        services.clock = clock  # the scheduler's view of "upcoming" follows the app clock
        self._active_jobs: set[tuple] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield BankrollHeader()
            yield QuotaBar()
        with Vertical(id="left"):
            yield BalanceChart()
            yield KpiTiles()
        with Vertical(id="centre"):
            yield OpportunityBoard()
            yield ScoresPanel()
        yield EventLog()
        with Horizontal(id="command-bar"):
            yield Input(id="cmd", placeholder="command (try help)", suggester=SuggestFromList(COMMAND_WORDS))
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.refresh_views)
        self.run_worker(self._startup(), group="startup", exclusive=True)

    async def on_unmount(self) -> None:
        await self.services.aclose()

    async def _startup(self) -> None:
        self.log_event("info", "starting: loading teams, games, models...")
        self.log_lines(await self.services.startup(self.clock()))
        self.refresh_views()
        self.set_interval(self.TICK_S, self.tick)

    # -- event log --------------------------------------------------------
    def log_event(self, kind: str, msg: str) -> None:
        self.query_one(EventLog).event(kind, msg)

    def log_lines(self, lines: list[tuple[str, str]]) -> None:
        for kind, msg in lines:
            self.log_event(kind, msg)

    # -- scheduling -------------------------------------------------------
    def tick(self) -> None:
        """Start each due job; one worker per job name runs that name's jobs in order."""
        by_name: dict[str, list[Job]] = {}
        for job in self.services.scheduler.due(self.clock()):
            if job.key not in self._active_jobs:
                by_name.setdefault(job.name, []).append(job)
        for name, jobs in by_name.items():
            if any(key[0] == name for key in self._active_jobs):
                continue  # the previous batch is still running; exclusive would cancel it
            self._spawn(jobs, name, exclusive=True)

    def _spawn(self, jobs: list[Job], group: str, exclusive: bool = False) -> None:
        self._active_jobs.update(j.key for j in jobs)
        self.run_worker(self._run_jobs(jobs), group=group, exclusive=exclusive)

    async def _run_jobs(self, jobs: list[Job]) -> None:
        try:
            for job in jobs:
                lines = await self.services.run_job(job, self.clock())
                self.services.scheduler.mark_done(job, self.clock())
                self.log_lines(lines)
                self.refresh_views()
        finally:
            self._active_jobs.difference_update(j.key for j in jobs)

    # -- views ------------------------------------------------------------
    def _board_recs(self, now: datetime) -> list[Recommendation]:
        recs = [r for r in self.services.lifecycle.board()
                if r.state in ("RECOMMENDED", "PENDING")
                or (r.state in ("LOGGED", "PLACED") and r.expires_at > now)]
        return sorted(recs, key=lambda r: r.id)

    def _bet_status(self) -> dict[str, str]:
        """Live games where an open single-leg Kalshi game bet is currently winning or losing."""
        out: dict[str, str] = {}
        for bet in self.services.lifecycle.open_bets():
            if bet["kind"] != "kalshi_game":
                continue
            leg = Leg(**json.loads(bet["legs_json"])[0])
            g = self.services.registry.get(leg.game_id or "")
            if g is None or g.status != "live":
                continue
            yes = _yes_from_score(leg, None, g)
            if yes is not None:
                out[g.game_id] = "winning" if yes == (leg.side == "yes") else "losing"
        return out

    def refresh_views(self) -> None:
        s, now = self.services, self.clock()

        def remaining(api: str) -> int | None:
            return s.budgets[api].remaining() if api in s.budgets else None

        self.query_one(BankrollHeader).set_balance(s.ledger.balance(), s.ledger.today_change(PT))
        self.query_one(QuotaBar).update_quota(remaining("odds_api"), remaining("cfbd"),
                                              remaining("therundown"), s.kalshi_ok)
        self.query_one(BalanceChart).update_series(s.ledger.history(), s.balance_markers())
        self.query_one(KpiTiles).update_kpis(s.kpis())
        self.query_one(OpportunityBoard).set_recs(self._board_recs(now))
        games = [g for g in s.registry.all_since(now - timedelta(hours=12))
                 if g.kickoff <= now + timedelta(days=7)]
        self.query_one(ScoresPanel).set_games(games[:60], self._bet_status(),
                                              s.rundown.delay_seconds if s.rundown else None)

    # -- commands -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "cmd":
            return
        event.stop()
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        try:
            self._dispatch(parse_command(text))
        except InsufficientFunds as e:
            self.log_event("error", _funds_error(e))
        except _USER_ERRORS as e:
            self.log_event("error", str(e))
        self.refresh_views()

    def _dispatch(self, cmd: Command) -> None:
        s, now, a = self.services, self.clock(), cmd.args
        match cmd.name:
            case "deposit":
                s.ledger.deposit(a["cents"], note="manual")
                self.log_event("deposit", f"{_money(a['cents'])} - balance {_money(s.ledger.balance())}")
            case "withdraw":
                s.ledger.withdraw(a["cents"], note="manual")
                self.log_event("withdraw", f"{_money(a['cents'])} - balance {_money(s.ledger.balance())}")
            case "confirm":
                self._confirm(a["id"], a["price"], a["qty"])
            case "skip":
                if s.lifecycle.get_rec(a["id"]) is None:
                    raise LifecycleError("unknown id")
                s.lifecycle.skip(a["id"])
                self.log_event("info", f"skipped #{a['id']}")
            case "scan":
                self.log_event("info", f"scanning {a['what']}...")
                self._spawn([Job("scan", args={"what": a["what"]})], "manual-scan")
            case "refresh":
                self._refresh(a["league"], a["force"])
            case "bets":
                self._bets(a["which"])
            case "settle":
                s.settler.manual_settle(a["id"], a["result"], a["cents"], now)
                self.log_event("settle", f"bet {a['id']} {a['result']} - balance {_money(s.ledger.balance())}")
            case "pp add":
                s.add_manual_pp(a["player"], a["stat"], a["line"], a["team"], a["game_id"], now)
                self.log_event("info", f"PrizePicks line added: {a['player']} {a['stat']} {a['line']}")
            case "quota":
                for line in s.quota_lines():
                    self.log_event("api", line)
            case "notify":
                self._notify(a["mode"], now)
            case "set":
                s.set_setting(a["field"], a["value"])
                self.log_event("info", f"{a['field']} = {getattr(s.settings, a['field'])}")
            case "venues":
                self.log_event("info", f"venues: {', '.join(s.settings.venues)} (change with: set venues a,b)")
            case "backtest":
                self.log_event("info", f"backtest {a['league']} {a['start']}-{a['end']} running...")
                self.run_worker(self._backtest(a["league"], a["start"], a["end"]), group="backtest")
            case "fit":
                self.log_event("info", "refitting the blend gates...")
                self.run_worker(self._fit_blend(), group="backtest")
            case "report":
                self._report()
            case "help":
                self.log_event("info", "commands: " + ", ".join(COMMAND_WORDS))

    def _confirm(self, rec_id: int, price: float | None, qty: int | None) -> None:
        bet_id = self.services.lifecycle.confirm(rec_id, self.clock(), price, qty)
        self.log_event("confirm", f"bet {bet_id} placed from #{rec_id} - balance "
                                  f"{_money(self.services.ledger.balance())}")

    def _refresh(self, league: str | None, force: bool) -> None:
        leagues = [league] if league else list(LEAGUES)
        jobs = [Job("kalshi_refresh")]
        for lg in leagues:
            jobs += [Job("rundown_scores", lg), Job("rundown_lines", lg)]
        cost = ODDS_GAMES_COST * len(leagues)
        if force:
            self.log_event("info", f"refresh --force: pulling sportsbook odds, ~{cost} Odds API credits")
            jobs += [Job("odds_games", lg, {"window": "manual"}, essential=True) for lg in leagues]
        else:
            self.log_event("info", f"refreshing Kalshi and TheRundown (add --force for sportsbook odds, "
                                   f"~{cost} credits)")
        self._spawn([*jobs, Job("scan")], "manual-refresh")

    def _bets(self, which: str) -> None:
        lc = self.services.lifecycle
        rows = lc.open_bets() if which == "open" else lc.settled_bets(10)
        if not rows:
            self.log_event("info", f"no {which} bets")
        for b in rows:
            legs = " + ".join(leg["description"] for leg in json.loads(b["legs_json"]))
            tail = (f"{b['result']} payout {_money(b['payout_cents'] or 0)}" if which == "settled"
                    else f"placed {b['placed_at'][:16]}")
            self.log_event("info", f"bet {b['id']} {b['kind']} {legs} stake "
                                   f"{_money(b['stake_cents'] + b['fee_cents'])} {tail}")

    def _notify(self, mode: str, now: datetime) -> None:
        if mode == "test":
            r = self.services.notify_test(now)
            if r.ok:
                self.log_event("text", f"test sent via {r.channel}"
                               + (": " + NOTIFY_TEST_TEXT if r.channel == "console" else ""))
            else:
                self.log_event("error", f"notify test failed ({r.channel}): {r.error}")
            return
        self.services.set_setting("notify_enabled", "true" if mode == "on" else "false")
        self.log_event("info", f"texts {mode}")

    def _report(self) -> None:
        k = self.services.kpis()
        band = "" if k["roi_band"] is None else f" ±{k['roi_band']:.1%}"
        brier = "n/a" if k["brier"] is None else f"{k['brier']:.3f}"
        self.log_event("info", f"report: ROI {_pct(k['roi'])}{band} | CLV {_pct(k['clv_mean'])} | "
                               f"record {k['record']} | max DD {_money(k['max_dd'])} | Brier {brier} | n={k['n']}")

    async def _backtest(self, league: str, start: int, end: int) -> None:
        try:
            fit, m = await self.services.backtest(league, start, end, self.clock())
        except Exception as e:
            self.log_event("error", f"backtest: {e}")
            return
        self.log_event("info", f"backtest {league} {start}-{end}: n={m['n']} ATS {m['ats_pct']:.1%} "
                               f"Brier {m['brier']:.3f} logloss {m['logloss']:.3f} "
                               f"b_model {fit.b_model:.2f}±{fit.b_model_se:.2f} "
                               f"-> {'observe-only' if fit.gated else 'live'}")

    async def _fit_blend(self) -> None:
        try:
            self.log_lines(await self.services.fit_gates(self.clock(), force=True))
        except Exception as e:
            self.log_event("error", f"fit blend: {e}")

    # -- confirm modal ------------------------------------------------------
    def on_opportunity_board_confirm_requested(self, msg: OpportunityBoard.ConfirmRequested) -> None:
        rec = self.services.lifecycle.get_rec(msg.rec_id)
        if rec is None or rec.state not in ("RECOMMENDED", "PENDING"):
            self.log_event("error", f"#{msg.rec_id} is not open for confirmation")
            return

        def done(result: tuple[float | None, int | None] | None) -> None:
            if result is None:
                self.log_event("info", f"#{rec.id} not confirmed")
                return
            try:
                self._confirm(rec.id, *result)
            except InsufficientFunds as e:
                self.log_event("error", _funds_error(e))
            except _USER_ERRORS as e:
                self.log_event("error", str(e))
            self.refresh_views()

        self.push_screen(ConfirmScreen(rec), done)
