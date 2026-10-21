"""The Textual app: focus-mode tabs (Home, Performance, Scores, Log), command bar, scheduler workers."""
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input, Static, TabbedContent, TabPane

from gridiron_edge import describe as d

from gridiron_edge.bets.lifecycle import LifecycleError
from gridiron_edge.bets.settle import _yes_from_score
from gridiron_edge.db import InsufficientFunds, InvalidAmount
from gridiron_edge.notify.format import PT
from gridiron_edge.scheduler import LEAGUES, Job
from gridiron_edge.services import NOTIFY_TEST_TEXT, Services
from gridiron_edge.types import Leg, Recommendation
from gridiron_edge.ui import home
from gridiron_edge.ui.commands import COMMAND_WORDS, HELP, Command, CommandError, parse_command
from gridiron_edge.ui.screens import ConfirmScreen, WhyScreen
from gridiron_edge.ui.widgets import (
    BalanceChart, BankrollHeader, EventLog, KpiTiles, OpenBetsList, PlaceNowList, QuotaBar,
    ScoresPanel, Section,
)

ODDS_GAMES_COST = 3  # credits per league for h2h+spreads+totals
_USER_ERRORS = (CommandError, LifecycleError, InvalidAmount, ValueError)


TABS = ("home", "performance", "scores", "log")


def _money(cents: int) -> str:
    return d.money(cents)


class CommandInput(Input):
    """The command bar. While it is empty, 1-4 switch tabs and Down jumps to the PLACE NOW list."""

    async def _on_key(self, event: events.Key) -> None:
        if not self.value and event.character in ("1", "2", "3", "4"):
            event.stop()
            event.prevent_default()
            self.app.action_show_tab(TABS[int(event.character) - 1])
            return
        if not self.value and event.key == "down":
            event.stop()
            event.prevent_default()
            self.app.action_focus_list()
            return
        await super()._on_key(event)


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
    BINDINGS = [
        Binding("1", "show_tab('home')", "Home"),
        Binding("2", "show_tab('performance')", "Performance"),
        Binding("3", "show_tab('scores')", "Scores"),
        Binding("4", "show_tab('log')", "Log"),
        Binding("colon,slash", "focus_command", "Type a command", show=False),
        Binding("escape", "focus_command", "Command bar", show=False),
    ]

    def __init__(self, services: Services, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        super().__init__()
        self.services = services
        self.clock = clock
        services.clock = clock  # the scheduler's view of "upcoming" follows the app clock
        self._active_jobs: set[tuple] = set()

    def compose(self) -> ComposeResult:
        yield BankrollHeader()
        with TabbedContent(id="tabs", initial="home"):
            with TabPane("1 Home", id="home"):
                yield Section("PLACE NOW", PlaceNowList(id="place-now"), "place-empty", id="place-box")
                yield Section("OPEN BETS", OpenBetsList(id="open-bets"), "open-empty", id="open-box")
            with TabPane("2 Performance", id="performance"):
                yield BalanceChart()
                yield KpiTiles()
                yield QuotaBar()
            with TabPane("3 Scores", id="scores"):
                yield ScoresPanel()
            with TabPane("4 Log", id="log"):
                yield EventLog()
        yield Static("", id="status")
        with Horizontal(id="command-bar"):
            yield CommandInput(id="cmd", placeholder="type a command (try help) - 1-4 switch tabs",
                               suggester=SuggestFromList(COMMAND_WORDS))
        yield Footer()

    # -- navigation -----------------------------------------------------------
    def action_show_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_focus_command(self) -> None:
        self.query_one("#cmd", Input).focus()

    def action_focus_list(self) -> None:
        self.action_show_tab("home")
        lst = self.query_one("#place-now", PlaceNowList)
        (lst if lst.display and lst.option_count else self.query_one("#open-bets", OpenBetsList)).focus()

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
        self.query_one("#status", Static).update(Text(f"{EventLog.LABELS.get(kind, kind)}: {msg}"))

    def log_lines(self, lines: list[tuple[str, str]]) -> None:
        for kind, msg in lines:
            self.log_event(kind, msg)

    # -- scheduling -------------------------------------------------------
    def tick(self) -> None:
        """Start each due job; one worker per job name runs that name's jobs in order."""
        self.refresh_home()  # countdowns and stale recs move with the clock
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

    @property
    def names(self) -> d.Names:
        return self.services.notifier.names

    def refresh_home(self) -> None:
        s, now = self.services, self.clock()
        games = {g.game_id: g for g in s.registry.all_since(now - timedelta(days=2))}
        markets = {m.ticker: m for m in s.latest.get("kalshi", [])}
        recs = home.actionable(s.lifecycle.board(), now, games, markets, s.settings.kalshi_min_ev)
        place = self.query_one("#place-box", Section)
        place.body.set_recs(recs, now, games, self.names)
        place.show_empty(None if recs else home.empty_reason(
            now, odds_key=s.odds is not None and not s.odds.disabled, games=list(games.values()),
            markets=list(markets.values()), gates=dict(s.gates), next_check=s.scheduler.next_check(now),
            names=self.names))
        views = [home.open_bet_view(b, games, markets, now, self.names, s._stat_lookup)
                 for b in s.lifecycle.open_bets()]
        bets = self.query_one("#open-box", Section)
        bets.body.set_bets(views)
        bets.show_empty(None if views else "No open bets. Bets you confirm show up here.")

    def _today_profit(self, now: datetime) -> int:
        """Today's win/loss: net result of every bet settled since PT midnight, whenever it was placed.
        Same as ledger moves since midnight (minus deposits/withdrawals) + cost of bets open now - cost
        of bets open at midnight, but read from the bets table so one clock (the app's) sets both edges.
        Money in still-open bets counts as not lost yet."""
        day = now.astimezone(PT).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(
            (b["payout_cents"] or 0) - b["stake_cents"] - b["fee_cents"]
            for b in self.services.conn.execute(
                "SELECT stake_cents, fee_cents, payout_cents, settled_at FROM bets "
                "WHERE state != 'PLACED' AND settled_at IS NOT NULL")
            if datetime.fromisoformat(b["settled_at"]) >= day)

    def refresh_views(self) -> None:
        s, now = self.services, self.clock()

        def remaining(api: str) -> int | None:
            return s.budgets[api].remaining() if api in s.budgets else None

        odds = (remaining("odds_api"), s.budgets["odds_api"].monthly_cap) if "odds_api" in s.budgets else None
        self.query_one(BankrollHeader).set_balance(s.ledger.balance(), self._today_profit(now),
                                                   s.lifecycle.open_exposure_cents(), odds)
        self.query_one(QuotaBar).update_quota(remaining("odds_api"), remaining("cfbd"),
                                              remaining("therundown"), s.kalshi_ok)
        self.query_one(BalanceChart).update_series(s.ledger.history(), s.balance_markers())
        self.query_one(KpiTiles).update_kpis(s.kpis())
        self.refresh_home()
        games = [g for g in s.registry.all_since(now - timedelta(hours=12))
                 if g.kickoff <= now + timedelta(days=7)]
        self.query_one(ScoresPanel).set_games(games[:60], self._bet_status(),
                                              s.rundown.delay_seconds if s.rundown else None, self.names)

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
                self.log_event("deposit", f"added {_money(a['cents'])} - bankroll now {_money(s.ledger.balance())}")
            case "withdraw":
                s.ledger.withdraw(a["cents"], note="manual")
                self.log_event("withdraw", f"took out {_money(a['cents'])} - bankroll now {_money(s.ledger.balance())}")
            case "confirm":
                self._confirm(a["id"], a["price"], a["qty"])
            case "skip":
                self._skip(a["id"])
            case "scan":
                self.log_event("info", "looking for bets now...")
                self._spawn([Job("scan", args={"what": a["what"]})], "manual-scan")
            case "refresh":
                self._refresh(a["league"], a["force"])
            case "bets":
                self._bets(a["which"])
            case "settle":
                s.settler.manual_settle(a["id"], a["result"], a["cents"], now)
                self.log_event("settle", f"bet {a['id']} marked {a['result']} - bankroll now {_money(s.ledger.balance())}")
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
                for usage, what in HELP:
                    self.log_event("info", f"{usage}  -  {what}")
                self.action_show_tab("log")

    def _confirm(self, rec_id: int, price: float | None, qty: int | None) -> None:
        bet_id = self.services.lifecycle.confirm(rec_id, self.clock(), price, qty)
        self.log_event("confirm", f"bet {bet_id} recorded from idea #{rec_id} - bankroll now "
                                  f"{_money(self.services.ledger.balance())}")

    def _skip(self, rec_id: int) -> None:
        if self.services.lifecycle.get_rec(rec_id) is None:
            raise LifecycleError("unknown id")
        self.services.lifecycle.skip(rec_id)
        self.log_event("info", f"skipped idea #{rec_id}")

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
            tail = (f"{b['result']}, paid {_money(b['payout_cents'] or 0)}" if which == "settled"
                    else f"placed {d.when(datetime.fromisoformat(b['placed_at']), self.clock())}")
            self.log_event("info", f"bet {b['id']}: {d.bet_row_text(b, self.names)} - "
                                   f"{_money(b['stake_cents'] + b['fee_cents'])} in, {tail}")

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
        band = "" if k["roi_band"] is None else f" (give or take {k['roi_band']:.1%})"
        won, lost, refunds = k["record"].split("-")
        self.log_event("info", f"report: return on money bet {_pct(k['roi'])}{band}; beat the closing "
                               f"price by {_pct(k['clv_mean'])} on average; {won} won, {lost} lost, "
                               f"{refunds} refunded; biggest drop {_money(k['max_dd'])}")
        self.action_show_tab("performance")

    async def _backtest(self, league: str, start: int, end: int) -> None:
        try:
            fit, m = await self.services.backtest(league, start, end, self.clock())
        except Exception as e:
            self.log_event("error", f"backtest: {e}")
            return
        verdict = "kept tracking-only (not proven yet)" if fit.gated else "allowed to suggest bets"
        self.log_event("info", f"backtest {league.upper()} {start}-{end}: {m['n']} games, picked the "
                               f"spread winner {m['ats_pct']:.1%} of the time, accuracy score "
                               f"{m['brier']:.3f} - our model is {verdict}")

    async def _fit_blend(self) -> None:
        try:
            self.log_lines(await self.services.fit_gates(self.clock(), force=True))
        except Exception as e:
            self.log_event("error", f"fit blend: {e}")

    # -- PLACE NOW keys: Enter confirm, s skip, d why ------------------------------
    def _open_rec(self, rec_id: int) -> Recommendation | None:
        rec = self.services.lifecycle.get_rec(rec_id)
        if rec is None or rec.state not in ("RECOMMENDED", "PENDING"):
            self.log_event("error", f"idea #{rec_id} can't be confirmed any more")
            self.refresh_home()
            return None
        return rec

    def on_place_now_list_confirm_requested(self, msg: PlaceNowList.ConfirmRequested) -> None:
        rec = self._open_rec(msg.rec_id)
        if rec is None:
            return

        def done(result: tuple[float | None, int | None] | None) -> None:
            if result is None:
                self.log_event("info", f"idea #{rec.id} not confirmed")
                return
            try:
                self._confirm(rec.id, *result)
            except InsufficientFunds as e:
                self.log_event("error", _funds_error(e))
            except _USER_ERRORS as e:
                self.log_event("error", str(e))
            self.refresh_views()

        self.push_screen(ConfirmScreen(rec, self.names), done)

    def on_place_now_list_skip_requested(self, msg: PlaceNowList.SkipRequested) -> None:
        try:
            self._skip(msg.rec_id)
        except _USER_ERRORS as e:
            self.log_event("error", str(e))
        self.refresh_home()

    def on_place_now_list_why_requested(self, msg: PlaceNowList.WhyRequested) -> None:
        rec = self.services.lifecycle.get_rec(msg.rec_id)
        if rec is None:
            return
        st = self.services.settings
        self.push_screen(WhyScreen(d.why_lines(rec, self.clock(), self.names, st.kelly_fraction, st.cap_bet)))
