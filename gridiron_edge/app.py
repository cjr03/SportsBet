"""The Textual app: focus-mode tabs (Home, Performance, Scores, Log, PrizePicks), command bar, scheduler workers."""
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input, Static, TabbedContent, TabPane

from gridiron_edge import describe as d

from gridiron_edge.bets.lifecycle import LifecycleError
from gridiron_edge.db import InsufficientFunds, InvalidAmount
from gridiron_edge.notify.format import PT
from gridiron_edge.bets.report import segment_label
from gridiron_edge.scheduler import LEAGUES, Job
from gridiron_edge.services import CHANNEL_WORDS, NOTIFY_TEST_TEXT, Services
from gridiron_edge.types import Recommendation
from gridiron_edge.ui import home
from gridiron_edge.ui import prizepicks as pp_ui
from gridiron_edge.ui.commands import COMMAND_WORDS, HELP, Command, CommandError, parse_command
from gridiron_edge.ui.screens import AskScreen, ConfirmScreen, WhyScreen
from gridiron_edge.ui.widgets import (
    BalanceChart, BankrollHeader, EventLog, InfoLine, KpiTiles, OpenBetsList, PerfMoney, PerfTables, PickList,
    PlaceNowList, QuotaBar, ScoresPanel, Section, WatchList,
)
from gridiron_edge.ui import scores as scores_ui
from gridiron_edge.pipeline import pp_entry_rec
from gridiron_edge.strategy.prizepicks import payout_table

_LEAGUE = {"nfl": "NFL", "cfb": "college football"}
ODDS_GAMES_COST = 3  # credits per league for h2h+spreads+totals
_USER_ERRORS = (CommandError, LifecycleError, InvalidAmount, ValueError)
# Plain words for the settings people change most; the rest read as their name with spaces
_SETTING_WORDS = {
    "kelly_fraction": "bet size (share of the ideal amount)",
    "cap_bet": "most of the bankroll on one bet",
    "cap_game": "most of the bankroll on one game",
    "cap_day": "most of the bankroll in one day",
    "cap_pp_daily": "most of the bankroll on PrizePicks in one day",
    "min_stake_cents": "smallest bet, in cents",
    "pp_max_per_game": "most PrizePicks picks from one game",
    "copula_rho": "how linked same-game picks are assumed to be",
    "props_stale_seconds": "how old player lines can be, in seconds",
}


TABS = ("home", "performance", "scores", "log", "prizepicks")


def _money(cents: int) -> str:
    return d.money(cents)


class PrizePicksPane(TabPane):
    """Tab 5. Keys while a pick list has focus: Enter/space add or remove a pick, d explains it,
    x clears YOUR ENTRY, c records an entry you placed on PrizePicks yourself, r loads new lines
    (asks first with the cost, like typing pp refresh)."""

    BINDINGS = [Binding("x", "app.pp_clear", "Clear entry"), Binding("c", "app.pp_confirm", "I placed this entry"),
                Binding("r", "app.pp_refresh_key", "Load new lines")]


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
        self._pp_selected: list[pp_ui.PickKey] = []  # YOUR ENTRY on tab 5, in the order picked
        self._pp_eval: pp_ui.EntryEval | None = None

    def compose(self) -> ComposeResult:
        yield BankrollHeader()
        yield InfoLine(id="info-line")
        with TabbedContent(id="tabs", initial="home"):
            with TabPane("1 Home", id="home"):
                yield Section("PLACE NOW", PlaceNowList(id="place-now"), "place-empty", id="place-box")
                yield Section("OPEN BETS", OpenBetsList(id="open-bets"), "open-empty", "open-summary",
                              id="open-box")
                yield Section("WATCHING (shown, not texted)", WatchList(id="watching"), "watch-empty",
                              "watch-summary", id="watch-box")
                yield Static("", id="pp-status")
            with TabPane("2 Performance", id="performance"):
                with Horizontal(id="perf-top"):
                    yield BalanceChart()
                    yield PerfMoney()
                with VerticalScroll(id="perf-scroll"):
                    yield PerfTables()
                    yield KpiTiles()
                    yield QuotaBar()
            with TabPane("3 Scores", id="scores"):
                yield ScoresPanel()
            with TabPane("4 Log", id="log"):
                yield EventLog()
            with PrizePicksPane("5 PrizePicks", id="prizepicks"):
                with Vertical(id="pp-summary-box") as box:
                    box.border_title = "PRIZEPICKS"
                    yield Static("", id="pp-summary")
                with Vertical(id="pp-best-box") as box:
                    box.border_title = "BEST PICKS NOW"
                    yield Static("No pick clears its needed chance yet.", id="pp-best-empty", classes="empty")
                    yield PickList(id="pp-best")
                with Vertical(id="pp-entry-box") as box:
                    box.border_title = "YOUR ENTRY"
                    with VerticalScroll(id="pp-entry-scroll"):  # focusable: arrows scroll a long entry
                        yield Static("", id="pp-entry")
                    yield Static("", id="pp-entry-keys")
                with Vertical(id="pp-scroll") as box:
                    box.border_title = "ALL LINES"
                    yield PickList(id="pp-board")
        yield Static("", id="status")
        with Horizontal(id="command-bar"):
            yield CommandInput(id="cmd", placeholder="Type a command (try: help). Keys 1-5 switch tabs.",
                               suggester=SuggestFromList(COMMAND_WORDS))
        yield Footer()

    # -- navigation -----------------------------------------------------------
    def action_show_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab
        if tab == "prizepicks":
            # From the command bar, focus stays there so letters keep typing (Down goes into the picks).
            # From a list (e.g. e on PLACE NOW), go straight into the picks so Enter/d/x/c work at once.
            if not isinstance(self.focused, Input):
                self._focus_pick_list()
        elif isinstance(self.focused, PickList):
            self.action_focus_command()

    def _focus_pick_list(self) -> bool:
        """Focus the first visible list with a line you can pick (highlighting that line), else ALL
        LINES (to scroll it). True when some line can be picked."""
        shown = [lst for lst in self.query(PickList) if lst.display and lst.parent.display]
        lst = next((lst for lst in shown if lst.selectable), shown[-1] if shown else None)
        if lst is not None:
            lst.focus()
            lst.highlight_first()
        return lst is not None and lst.selectable

    def action_focus_command(self) -> None:
        self.query_one("#cmd", Input).focus()

    def action_focus_list(self) -> None:
        """Down in the empty command bar: into the PrizePicks lists on tab 5, else Home's PLACE NOW."""
        if self.query_one("#tabs", TabbedContent).active == "prizepicks":
            if not self._focus_pick_list():
                self.log_event("info", pp_ui.nothing_to_pick_text(self._pp_view))
            return
        self.action_show_tab("home")
        lst = self.query_one("#place-now", PlaceNowList)
        (lst if lst.display and lst.option_count else self.query_one("#open-bets", OpenBetsList)).focus()

    # A shorter tab 5 below this many rows: BEST PICKS NOW steps aside while YOUR ENTRY has picks
    PP_SHORT_ROWS = 45

    def on_resize(self, event: events.Resize) -> None:
        if self._pp_view is not None:
            self._show_pp(self._pp_view.active, self.clock())

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
        self.query_one(EventLog).event(kind, msg, self.clock())
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
    @property
    def names(self) -> d.Names:
        return self.services.notifier.names

    def refresh_home(self) -> None:
        s, now = self.services, self.clock()
        games = {g.game_id: g for g in s.registry.all_since(now - timedelta(days=2))}
        markets = {m.ticker: m for m in s.latest.get("kalshi", [])}
        board = s.lifecycle.board(300)
        recs = home.actionable(board, now, games, markets, s.settings.kalshi_min_ev)
        budget, paused = s.place_limits(now)
        day_left, day_cap = budget.day_left(), math.floor(s.settings.cap_day * budget.bankroll)
        recs = home.within_limits(recs, games, budget, paused)
        try:
            payouts = payout_table(s.settings)
        except ValueError:  # a bad payout override: fall back to the standard tables
            payouts = None
        place = self.query_one("#place-box", Section)
        details = {r.id: home.place_now_detail(r, markets, games, s.settings.kalshi_min_ev, payouts) for r in recs}
        place.body.set_recs(recs, now, games, self.names, details)
        place.show_empty(None if recs else home.empty_reason(
            now, odds_key=s.odds is not None and not s.odds.disabled, games=list(games.values()),
            markets=list(markets.values()), gates=dict(s.gates), next_check=s.scheduler.next_check(now),
            names=self.names))
        views = [home.open_bet_view(b, games, markets, now, self.names, s._stat_lookup, payouts)
                 for b in s.lifecycle.open_bets()]
        bets = self.query_one("#open-box", Section)
        bets.body.set_bets(views)
        bets.show_empty(None if views else "No open bets. Bets you confirm show up here.")
        bets.show_summary(home.open_summary(views))
        self._show_watching(board, now, games, paused)
        self._show_info(now, day_left, day_cap)
        self._show_pp(sum(r.kind == "pp_entry" for r in recs), now)

    def _show_watching(self, board: list[Recommendation], now: datetime, games: dict, paused: dict) -> None:
        s = self.services
        watch = home.watching(board, now, games)
        shown = watch[:home.WATCH_LIMIT]
        rows = [home.watch_cells(r, now, games, home.watch_reason(r, games, dict(s.gates), paused,
                                                                  s.settings.middles_live), self.names)
                for r in shown]
        box = self.query_one("#watch-box", Section)
        box.body.set_rows(rows)
        summary = home.watch_summary(len(shown), len(watch))
        box.show_empty(summary if not watch else None)
        box.show_summary(summary if watch else None)

    def _show_info(self, now: datetime, day_left: int, day_cap: int) -> None:
        s = self.services

        def newest(items) -> datetime | None:
            stamps = [getattr(i, "fetched_at", None) or getattr(i, "last_update", None) for i in items]
            stamps = [t for t in stamps if t is not None]
            return max(stamps) if stamps else None

        self.query_one(InfoLine).set_segments(home.info_segments(
            now, record=self._today_record(now), day_left=day_left, day_cap=day_cap,
            next_check=s.scheduler.next_check(now), kalshi_at=newest(s.latest.get("kalshi", [])),
            books_at=newest(s.latest.get("quotes", [])), props_at=newest(s.latest.get("props", []))))

    def _show_pp(self, active: int, now: datetime) -> None:
        """The Home PrizePicks status line and the PrizePicks tab."""
        view = self._pp_view
        if view is None:
            return
        view.active = active
        self.query_one("#pp-status", Static).update(Text(pp_ui.status_line(view, now, self.names)))
        summary, _, lines = pp_ui.board_sections(view, now, self.names)
        if self._pp_selected and len(summary) > 2:  # room for the entry: just where things stand
            summary = summary[-2:]
        self.query_one("#pp-summary", Static).update(Text("\n".join(summary)))
        kept, dropped = pp_ui.reconcile(self._pp_selected, view)
        self._pp_selected = kept
        for key in dropped:
            self.log_event("warn", pp_ui.dropped_text(key))
        chosen = set(kept)
        best = pp_ui.best_rows(view, chosen, self.names)
        self.query_one("#pp-best", PickList).set_rows(best)
        self.query_one("#pp-best", PickList).display = bool(best)
        self.query_one("#pp-best-empty", Static).update(pp_ui.best_empty_text(view))
        self.query_one("#pp-best-empty").display = not best
        self.query_one("#pp-board", PickList).set_rows(pp_ui.all_rows(view, chosen, now, self.names))
        for box in ("#pp-best-box", "#pp-entry-box", "#pp-scroll"):
            self.query_one(box).display = bool(lines)
        short = self.size.height < self.PP_SHORT_ROWS
        self.query_one("#prizepicks").set_class(short, "-short")
        if short and kept:  # the picks are in ALL LINES too; keep YOUR ENTRY and ALL LINES on screen
            self.query_one("#pp-best-box").display = False
        self._show_pp_entry(view)
        if isinstance(self.focused, PickList) and not (self.focused.display and self.focused.parent.display):
            shown = any(lst.display and lst.parent.display for lst in self.query(PickList))
            if not shown:
                self.action_focus_command()
            else:
                self._focus_pick_list()

    def _pp_picks(self, view: pp_ui.PPView) -> list:
        live = pp_ui.pickable(view)
        return [live[k] for k in self._pp_selected if k in live]

    def _pp_match(self) -> Recommendation | None:
        """A live built PrizePicks idea with exactly the picks in YOUR ENTRY (its id is reused)."""
        keys, now = set(self._pp_selected), self.clock()
        return next((r for r in self.services.lifecycle.pending()
                     if r.kind == "pp_entry" and not r.observe_only and now < r.expires_at
                     and {pp_ui.leg_pick_key(leg) for leg in r.legs} == keys), None)

    def _show_pp_entry(self, view: pp_ui.PPView) -> None:
        picks = self._pp_picks(view)
        ev = pp_ui.evaluate_entry(picks, view, self.names)
        self._pp_eval = ev
        out = pp_ui.entry_lines(ev, view, self.names, waiting=len(self._pp_selected) - len(picks), now=self.clock())
        match = self._pp_match() if ev.picks else None
        if match is not None:
            out.insert(len(ev.picks), pp_ui.same_picks_text(match.id))
        box = self.query_one("#pp-entry-box")
        n = len(self._pp_selected)
        box.border_title = f"YOUR ENTRY ({n} pick{'s' * (n != 1)})" if n else "YOUR ENTRY"
        keys = out.pop() if n else ""  # the keys line stays in view below the scrolling entry
        self.query_one("#pp-entry", Static).update(Text("\n".join(out)))
        self.query_one("#pp-entry-keys", Static).update(Text(keys))
        self.query_one("#pp-entry-keys").display = bool(keys)

    # -- PrizePicks tab keys: Enter/space toggle, d why, x clear, c record ---------------------------
    def on_pick_list_toggle_requested(self, msg: PickList.ToggleRequested) -> None:
        view = self._pp_view
        if view is None:
            return
        live = pp_ui.pickable(view)
        if msg.key in self._pp_selected:
            self._pp_selected.remove(msg.key)
            self.log_event("info", pp_ui.removed_text(pp_ui.pick_words(live[msg.key]) if msg.key in live
                                                      else msg.key[0]))
        elif msg.key not in live:
            self.log_event("error", pp_ui.CANT_PICK)
            return
        else:
            ln = live[msg.key]
            problem = pp_ui.add_problem(self._pp_picks(view), ln, view, self.names)
            if problem is not None:
                self.log_event("error", problem)
                return
            self._pp_selected.append(msg.key)
            self.log_event("info", pp_ui.added_text(ln, len(self._pp_selected)))
        self._show_pp(view.active, self.clock())

    def on_pick_list_nothing_to_pick(self, msg: PickList.NothingToPick) -> None:
        self.log_event("info", pp_ui.nothing_to_pick_text(self._pp_view))

    def action_pp_refresh_key(self) -> None:
        """Key r on tab 5: the same as typing pp refresh, but asks yes or no with the cost first."""
        games, text = self.services.pp_refresh_plan(self.clock())
        if not games:
            self.log_event("info", text)
            return
        n = len(games)
        question = (f"Load new PrizePicks lines now for {n} game{'s' * (n != 1)}? "
                    + text.replace(" Type: pp refresh yes", ""))

        def done(yes: bool | None) -> None:
            if yes:
                self._pp_refresh(True)
            else:
                self.log_event("info", "PrizePicks lines not loaded; no credits used.")

        self.push_screen(AskScreen(question, yes="Yes, load them", no="No"), done)

    def on_pick_list_why_requested(self, msg: PickList.WhyRequested) -> None:
        view = self._pp_view
        ln = next((ln for ln in view.lines if pp_ui.pick_key(ln) == msg.key), None) if view else None
        if ln is not None:
            self.push_screen(WhyScreen(pp_ui.pick_why_lines(ln, view, self.clock(), self.names),
                                       title="Why this pick?"))

    def action_pp_clear(self) -> None:
        if not self._pp_selected:
            self.log_event("info", pp_ui.ALREADY_EMPTY)
            return
        self._pp_selected = []
        self.log_event("info", pp_ui.CLEARED)
        if self._pp_view is not None:
            self._show_pp(self._pp_view.active, self.clock())

    def action_pp_confirm(self) -> None:
        """Record a PrizePicks entry you placed yourself: the confirm modal, then a pp_entry idea and bet."""
        view, ev = self._pp_view, self._pp_eval
        if view is None or ev is None or not ev.picks:
            self.log_event("error", pp_ui.WAIT_FOR_LINES if self._pp_selected else pp_ui.EMPTY_ENTRY)
            return
        if not ev.ready:
            self.log_event("error", ev.problems[0] if ev.problems else "This entry can't be worked out.")
            return
        now, st = self.clock(), self.services.settings
        match = self._pp_match()
        rec = match or pp_entry_rec(ev.legs, ev.structure, ev.ev or 0.0, ev.stake_cents, False, payout_table(st),
                                    now, view.games, st.copula_rho)
        keys = list(self._pp_selected)

        def done(result: tuple[float | None, int | None, int | None] | None) -> None:
            if result is None:
                self.log_event("info", pp_ui.NOT_RECORDED)
                return
            try:
                if match is None:  # idea and bet saved together: a failure leaves no stray idea in PLACE NOW
                    rec_id, bet_id = self.services.lifecycle.record_manual(rec, self.clock(), result[2])
                    self._log_recorded(bet_id, rec_id)
                else:
                    self._confirm(match.id, None, None, result[2])
            except InsufficientFunds as e:
                self.log_event("error", _funds_error(e))
            except _USER_ERRORS as e:
                self.log_event("error", error_text(e))
            else:
                if self._pp_selected == keys:
                    self._pp_selected = []
            self.refresh_views()

        self.push_screen(ConfirmScreen(rec, self.names, warning=pp_ui.old_prices_text(ev, now)), done)

    def on_place_now_list_load_entry_requested(self, msg: PlaceNowList.LoadEntryRequested) -> None:
        rec = self.services.lifecycle.get_rec(msg.rec_id)
        if rec is None or rec.kind != "pp_entry":
            self.log_event("error", pp_ui.ONLY_PP_OPENS)
            return
        self._pp_selected = [pp_ui.leg_pick_key(leg) for leg in rec.legs]
        self.log_event("info", pp_ui.opened_text(rec.id))
        if self._pp_view is not None:
            self._show_pp(self._pp_view.active, self.clock())
        self.action_show_tab("prizepicks")

    def _settled_today(self, now: datetime) -> list:
        day = now.astimezone(PT).replace(hour=0, minute=0, second=0, microsecond=0)
        return [b for b in self.services.conn.execute(
            "SELECT result, stake_cents, fee_cents, payout_cents, settled_at FROM bets "
            "WHERE state != 'PLACED' AND settled_at IS NOT NULL")
            if datetime.fromisoformat(b["settled_at"]) >= day]

    def _today_record(self, now: datetime) -> tuple[int, int, int]:
        """(won, lost, refunded) among bets settled since PT midnight."""
        rows = self._settled_today(now)
        return tuple(sum(b["result"] == r for b in rows) for r in ("won", "lost", "refund"))  # type: ignore[return-value]

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
            mv = s.money_view(now)
            self.query_one(PerfMoney).update_money(mv["windows"], mv["deposited"], mv["withdrawn"])
            self.query_one(PerfTables).update_tables(mv["by_type"], mv["recent"], now, self.names)
        except Exception as e:  # never let the tables take the dashboard down
            self.query_one(PerfTables).update(Text(f"Could not show the bet tables ({e})."))
        try:
            self._pp_view = s.pp_view(now)
        except Exception as e:  # never let the PrizePicks summary take the dashboard down
            self._pp_view = None
            self.query_one("#pp-status", Static).update(Text(f"PrizePicks: could not show the lines ({e})."))
        self.refresh_home()
        self._show_scores(now)

    def _show_scores(self, now: datetime) -> None:
        """Scores tab from data already loaded: games, sportsbook lines, Kalshi prices, our chances."""
        s = self.services
        games = [g for g in s.registry.all_since(now - timedelta(hours=12))
                 if g.kickoff <= now + timedelta(days=7)]
        try:
            chances = s.win_chances(now)
        except Exception:  # noqa: BLE001 - the tab still shows scores and prices
            chances = {}
        counts = scores_ui.bet_counts(s.lifecycle.open_bets(), lambda b: json.loads(b["legs_json"]))
        rows = scores_ui.score_rows(games, now, quotes=s.latest.get("quotes", []),
                                    markets=s.latest.get("kalshi", []), chances=chances, bet_counts=counts,
                                    bet_status=scores_ui.live_standing(s.lifecycle.open_bets(),
                                                                       {g.game_id: g for g in games}),
                                    names=self.names)
        self.query_one(ScoresPanel).set_rows(rows, s.rundown.delay_seconds if s.rundown else None)

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
                label = _SETTING_WORDS.get(a["field"], a["field"].replace("_", " "))
                self.log_event("info", f"Setting changed: {label} is now "
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
                for usage, what in HELP:  # "Type: ..." so the auto-capital never lands on a command
                    self.log_event("info", f"Keys: {what}" if usage == "keys"
                                   else f"Type: {usage}  -  {what[:1].upper() + what[1:]}")
                self.action_show_tab("log")

    def _confirm(self, rec_id: int, price: float | None, qty: int | None, stake_cents: int | None = None) -> None:
        bet_id = self.services.lifecycle.confirm(rec_id, self.clock(), price, qty, stake_cents)
        self._log_recorded(bet_id, rec_id)

    def _log_recorded(self, bet_id: int, rec_id: int) -> None:
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
        verdict = "kept watch-only (shown, not texted; not proven yet)" if fit.gated else "allowed to suggest bets"
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

        def done(result: tuple[float | None, int | None, int | None] | None) -> None:
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
