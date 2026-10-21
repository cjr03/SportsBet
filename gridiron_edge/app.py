"""The Textual app: focus-mode tabs (Home, Performance, Scores, Log, PrizePicks), command bar, scheduler workers."""
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input, Static, TabbedContent, TabPane

from gridiron_edge import describe as d

from gridiron_edge.bets.lifecycle import LifecycleError
from gridiron_edge.bets.settle import _yes_from_score
from gridiron_edge.db import InsufficientFunds, InvalidAmount
from gridiron_edge.notify.format import PT
from gridiron_edge.bets.report import segment_label
from gridiron_edge.scheduler import LEAGUES, Job
from gridiron_edge.services import CHANNEL_WORDS, NOTIFY_TEST_TEXT, Services
from gridiron_edge.types import Leg, Recommendation
from gridiron_edge.ui import home
from gridiron_edge.ui import prizepicks as pp_ui
from gridiron_edge.ui.commands import COMMAND_WORDS, HELP, Command, CommandError, parse_command
from gridiron_edge.ui.screens import ConfirmScreen, WhyScreen
from gridiron_edge.ui.widgets import (
    BalanceChart, BankrollHeader, EventLog, KpiTiles, OpenBetsList, PlaceNowList, QuotaBar,
    ScoresPanel, Section,
)

_LEAGUE = {"nfl": "NFL", "cfb": "college football"}
ODDS_GAMES_COST = 3  # credits per league for h2h+spreads+totals
_USER_ERRORS = (CommandError, LifecycleError, InvalidAmount, ValueError)


TABS = ("home", "performance", "scores", "log", "prizepicks")


def _money(cents: int) -> str:
    return d.money(cents)


class CommandInput(Input):
    """The command bar. While it is empty, 1-5 switch tabs and Down jumps to the PLACE NOW list."""

    async def _on_key(self, event: events.Key) -> None:
        if not self.value and event.character in ("1", "2", "3", "4", "5"):
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
    return msg if msg.startswith("Not enough money") else f"Not enough money in the bankroll: {msg}"


# lifecycle errors are short codes; say them as sentences
_ERROR_WORDS = {
    "unknown id": "There is no bet idea or bet with that number.",
    "not pending": "That bet idea can't be confirmed any more (it was already placed, skipped or replaced).",
    "expired": "That bet idea has gone stale; wait for a fresh one.",
    "bet is not open": "That bet is already settled.",
}


def error_text(e: Exception) -> str:
    """A user error as one plain sentence starting with a capital letter."""
    msg = str(e)
    msg = _ERROR_WORDS.get(msg, msg)
    return msg[:1].upper() + msg[1:]


def _settle_word(result: str) -> str:
    return {"won": "won", "lost": "lost", "refund": "refunded"}.get(result, result)


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
        Binding("5", "show_tab('prizepicks')", "PrizePicks"),
        Binding("colon,slash", "focus_command", "Type a command", show=False),
        Binding("escape", "focus_command", "Command bar", show=False),
    ]

    def __init__(self, services: Services, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        super().__init__()
        self.services = services
        self.clock = clock
        services.clock = clock  # the scheduler's view of "upcoming" follows the app clock
        self._active_jobs: set[tuple] = set()
        self._pp_view: pp_ui.PPView | None = None  # rebuilt with the other views, not every tick

    def compose(self) -> ComposeResult:
        yield BankrollHeader()
        with TabbedContent(id="tabs", initial="home"):
            with TabPane("1 Home", id="home"):
                yield Section("PLACE NOW", PlaceNowList(id="place-now"), "place-empty", id="place-box")
                yield Section("OPEN BETS", OpenBetsList(id="open-bets"), "open-empty", id="open-box")
                yield Static("", id="pp-status")
            with TabPane("2 Performance", id="performance"):
                yield BalanceChart()
                yield KpiTiles()
                yield QuotaBar()
            with TabPane("3 Scores", id="scores"):
                yield ScoresPanel()
            with TabPane("4 Log", id="log"):
                yield EventLog()
            with TabPane("5 PrizePicks", id="prizepicks"):
                with VerticalScroll(id="pp-scroll"):
                    yield Static("", id="pp-board")
        yield Static("", id="status")
        with Horizontal(id="command-bar"):
            yield CommandInput(id="cmd", placeholder="Type a command (try: help). Keys 1-5 switch tabs.",
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
        self.log_event("info", "Starting up: loading teams, games and team ratings...")
        self.log_lines(await self.services.startup(self.clock()))
        self.refresh_views()
        self.set_interval(self.TICK_S, self.tick)

    # -- event log --------------------------------------------------------
    def log_event(self, kind: str, msg: str) -> None:
        msg = msg[:1].upper() + msg[1:]  # every log line reads as a sentence
        self.query_one(EventLog).event(kind, msg)
        self.query_one("#status", Static).update(Text(f"{EventLog.LABELS.get(kind, kind).capitalize()}: {msg}"))

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
        budget, paused = s.place_limits(now)
        recs = home.within_limits(recs, games, budget, paused)
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
        self._show_pp(sum(r.kind == "pp_entry" for r in recs), now)

    def _show_pp(self, active: int, now: datetime) -> None:
        """The Home PrizePicks status line and the PrizePicks tab."""
        view = self._pp_view
        if view is None:
            return
        view.active = active
        self.query_one("#pp-status", Static).update(Text(pp_ui.status_line(view, now, self.names)))
        self.query_one("#pp-board", Static).update(Text("\n".join(pp_ui.board_lines(view, now, self.names))))

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
        self.query_one(KpiTiles).update_kpis(s.performance(now))
        try:
            self._pp_view = s.pp_view(now)
        except Exception as e:  # never let the PrizePicks summary take the dashboard down
            self._pp_view = None
            self.query_one("#pp-status", Static).update(Text(f"PrizePicks: could not show the lines ({e})."))
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
            self.log_event("error", error_text(e))
        self.refresh_views()

    def _dispatch(self, cmd: Command) -> None:
        s, now, a = self.services, self.clock(), cmd.args
        match cmd.name:
            case "deposit":
                s.ledger.deposit(a["cents"], note="manual")
                self.log_event("deposit", f"Added {_money(a['cents'])}. Bankroll is now {_money(s.ledger.balance())}.")
            case "withdraw":
                s.ledger.withdraw(a["cents"], note="manual")
                self.log_event("withdraw", f"Took out {_money(a['cents'])}. Bankroll is now {_money(s.ledger.balance())}.")
            case "confirm":
                self._confirm(a["id"], a["price"], a["qty"])
            case "skip":
                self._skip(a["id"])
            case "scan":
                self.log_event("info", "Looking for bets now...")
                self._spawn([Job("scan", args={"what": a["what"]})], "manual-scan")
            case "refresh":
                self._refresh(a["league"], a["force"])
            case "bets":
                self._bets(a["which"])
            case "settle":
                s.settler.manual_settle(a["id"], a["result"], a["cents"], now)
                self.log_event("settle", f"Bet #{a['id']} marked as {_settle_word(a['result'])}. "
                                         f"Bankroll is now {_money(s.ledger.balance())}.")
            case "pp refresh":
                self._pp_refresh(a["confirmed"])
            case "pp add":
                s.add_manual_pp(a["player"], a["stat"], a["line"], a["team"], a["game_id"], now)
                self.log_event("info", f"PrizePicks line added: {a['player']} {a['line']:g} {d.stat_words(a['stat'])}.")
            case "quota":
                for line in s.quota_lines():
                    self.log_event("api", line)
            case "notify":
                self._notify(a["mode"], now)
            case "set":
                s.set_setting(a["field"], a["value"])
                self.log_event("info", f"Setting changed: {a['field'].replace('_', ' ')} is now "
                                       f"{getattr(s.settings, a['field'])}.")
            case "venues":
                sites = ", ".join(d.book_name(v) for v in s.settings.venues)
                self.log_event("info", f"Betting sites in use: {sites} (change with: set venues kalshi,prizepicks).")
            case "backtest":
                self.log_event("info", f"Testing the {_LEAGUE[a['league']]} model on {a['start']}-{a['end']} games...")
                self.run_worker(self._backtest(a["league"], a["start"], a["end"]), group="backtest")
            case "fit":
                self.log_event("info", "Rechecking which kinds of bets our model has earned...")
                self.run_worker(self._fit_blend(), group="backtest")
            case "report":
                self._report()
            case "drift":
                names = s.reset_drift(a["segment"])
                if not names:
                    self.log_event("info", "Nothing was paused.")
                for name in names:
                    label = segment_label(name)
                    self.log_event("info", f"{label[:1].upper() + label[1:]} are back on (texted again).")
            case "help":
                for usage, what in HELP:
                    self.log_event("info", f"{usage}  -  {what[:1].upper() + what[1:]}")
                self.action_show_tab("log")

    def _confirm(self, rec_id: int, price: float | None, qty: int | None) -> None:
        bet_id = self.services.lifecycle.confirm(rec_id, self.clock(), price, qty)
        self.log_event("confirm", f"Bet #{bet_id} recorded from idea #{rec_id}. Bankroll is now "
                                  f"{_money(self.services.ledger.balance())}.")

    def _skip(self, rec_id: int) -> None:
        if self.services.lifecycle.get_rec(rec_id) is None:
            raise LifecycleError("unknown id")
        self.services.lifecycle.skip(rec_id)
        self.log_event("info", f"Skipped bet idea #{rec_id}.")

    def _refresh(self, league: str | None, force: bool) -> None:
        leagues = [league] if league else list(LEAGUES)
        jobs = [Job("kalshi_refresh")]
        for lg in leagues:
            jobs += [Job("rundown_scores", lg), Job("rundown_lines", lg)]
        cost = ODDS_GAMES_COST * len(leagues)
        if force:
            self.log_event("info", f"Refreshing everything, including sportsbook prices (about {cost} Odds API "
                                   "credits)...")
            jobs += [Job("odds_games", lg, {"window": "manual"}, essential=True) for lg in leagues]
        else:
            self.log_event("info", "Refreshing Kalshi prices and scores... (type refresh --force to also load "
                                   f"sportsbook prices, about {cost} Odds API credits)")
        self._spawn([*jobs, Job("scan")], "manual-refresh")

    def _pp_refresh(self, confirmed: bool) -> None:
        games, text = self.services.pp_refresh_plan(self.clock())
        if not confirmed or not games:
            self.log_event("info", text)
            return
        self.log_event("info", f"Loading PrizePicks lines for {len(games)} game{'s' * (len(games) != 1)}...")
        self.run_worker(self._run_pp_refresh(), group="pp-refresh", exclusive=True)

    async def _run_pp_refresh(self) -> None:
        self.log_lines(await self.services.pp_refresh(self.clock()))
        self.refresh_views()

    def _bets(self, which: str) -> None:
        lc = self.services.lifecycle
        rows = lc.open_bets() if which == "open" else lc.settled_bets(10)
        if not rows:
            self.log_event("info", f"No {which} bets.")
        for b in rows:
            tail = (f"{_settle_word(b['result'])}, {_money(b['payout_cents'] or 0)} paid back" if which == "settled"
                    else f"placed {d.when(datetime.fromisoformat(b['placed_at']), self.clock())} PT")
            self.log_event("info", f"Bet #{b['id']}: {d.bet_row_text(b, self.names)} - "
                                   f"{_money(b['stake_cents'] + b['fee_cents'])} in, {tail}.")

    def _notify(self, mode: str, now: datetime) -> None:
        if mode == "test":
            r = self.services.notify_test(now)
            if r.ok:
                self.log_event("text", f"Test text sent via {CHANNEL_WORDS.get(r.channel, r.channel)}"
                               + (": " + NOTIFY_TEST_TEXT if r.channel == "console" else "."))
            else:
                self.log_event("error", f"The test text did not go out ({CHANNEL_WORDS.get(r.channel, r.channel)}): "
                                        f"{r.error}")
            return
        self.services.set_setting("notify_enabled", "true" if mode == "on" else "false")
        self.log_event("info", f"Texts turned {mode}.")

    def _report(self) -> None:
        for line in self.services.report(self.clock())["summary"]:
            self.log_event("warn" if line.startswith("Warning") else "info", line)
        self.action_show_tab("performance")

    async def _backtest(self, league: str, start: int, end: int) -> None:
        try:
            fit, m = await self.services.backtest(league, start, end, self.clock())
        except Exception as e:
            self.log_event("error", f"Could not test the model on past games: {e}")
            return
        verdict = "kept tracking-only (not proven yet)" if fit.gated else "allowed to suggest bets"
        self.log_event("info", f"{_LEAGUE[league]} model test, {start}-{end}: {m['n']:,} games; it picked the "
                               f"point-spread winner {m['ats_pct']:.1%} of the time (accuracy score "
                               f"{m['brier']:.3f}, lower is better; 0.250 is a coin flip). Our model is {verdict}.")

    async def _fit_blend(self) -> None:
        try:
            self.log_lines(await self.services.fit_gates(self.clock(), force=True))
        except Exception as e:
            self.log_event("error", f"Could not recheck the model: {e}")

    # -- PLACE NOW keys: Enter confirm, s skip, d why ------------------------------
    def _open_rec(self, rec_id: int) -> Recommendation | None:
        rec = self.services.lifecycle.get_rec(rec_id)
        if rec is None or rec.state not in ("RECOMMENDED", "PENDING"):
            self.log_event("error", f"Bet idea #{rec_id} can't be confirmed any more (it was placed, skipped or "
                                    "went stale).")
            self.refresh_home()
            return None
        return rec

    def on_place_now_list_confirm_requested(self, msg: PlaceNowList.ConfirmRequested) -> None:
        rec = self._open_rec(msg.rec_id)
        if rec is None:
            return

        def done(result: tuple[float | None, int | None] | None) -> None:
            if result is None:
                self.log_event("info", f"Bet idea #{rec.id} not confirmed.")
                return
            try:
                self._confirm(rec.id, *result)
            except InsufficientFunds as e:
                self.log_event("error", _funds_error(e))
            except _USER_ERRORS as e:
                self.log_event("error", error_text(e))
            self.refresh_views()

        self.push_screen(ConfirmScreen(rec, self.names), done)

    def on_place_now_list_skip_requested(self, msg: PlaceNowList.SkipRequested) -> None:
        try:
            self._skip(msg.rec_id)
        except _USER_ERRORS as e:
            self.log_event("error", error_text(e))
        self.refresh_home()

    def on_place_now_list_why_requested(self, msg: PlaceNowList.WhyRequested) -> None:
        rec = self.services.lifecycle.get_rec(msg.rec_id)
        if rec is None:
            return
        st = self.services.settings
        self.push_screen(WhyScreen(d.why_lines(rec, self.clock(), self.names, st.kelly_fraction, st.cap_bet,
                                               settings=st)))
