"""TUI widgets: header, PLACE NOW / OPEN BETS lists, chart, KPI tiles, quota bars, scores, event log."""
import math
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import OptionList, RichLog, Static
from textual.widgets.option_list import Option
from textual_plotext import PlotextPlot

from gridiron_edge.bets import report
from gridiron_edge import describe as d
from gridiron_edge.types import Recommendation
from gridiron_edge.ui.home import OpenBetView, pack, place_now_cells
from gridiron_edge.ui import performance as perf
from gridiron_edge.ui.prizepicks import PickKey, PickRow
from gridiron_edge.ui.scores import ScoreRow

PROFIT, LOSS, PENDING, ACCENT = "#22c55e", "#ef4444", "#f59e0b", "#22d3ee"
_RGB = {"profit": (34, 197, 94), "loss": (239, 68, 68)}
_SURFACE_RGB = (18, 24, 41)
MIN_CONCLUSIVE_N = 1500  # unused: kept for importers; the verdict now comes from the ROI CI


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def compute_kpis(settled_rows, history: list[tuple[datetime, int]] | None = None) -> dict:
    """KPIs from settled bet rows (dicts or sqlite rows); see bets/report.py for the definitions.

    ROI is net of Kalshi fees and excludes arbs (reported as realized profit). Max drawdown is on
    settled trading P&L, not the ledger balance, so `history` is no longer used.
    """
    rows = list(settled_rows)
    bets = [r for r in rows if _get(r, "kind") != "arb"]
    arbs = [r for r in rows if _get(r, "kind") == "arb" and _get(r, "result") in ("won", "lost", "refund")]
    roi = report.roi_stats(bets)
    clv = report.clv_stats(_get(r, "clv") for r in rows if _get(r, "result") in ("won", "lost"))
    dd = report.drawdown(rows)
    won = sum(_get(r, "result") == "won" for r in rows)
    lost = sum(_get(r, "result") == "lost" for r in rows)
    refunds = sum(_get(r, "result") == "refund" for r in rows)
    return {
        "roi": roi["roi"], "roi_band": None if roi["se"] is None else report.Z * roi["se"],
        "roi_ci": (roi["ci_lo"], roi["ci_hi"]), "conclusive": roi["conclusive"],
        "clv_mean": clv["mean"], "clv_pos": clv["pos_share"],
        "record": f"{won}-{lost}-{refunds}", "max_dd": dd["max_dd_cents"], "current_dd": dd["current_dd_cents"],
        "brier": report.brier(rows), "brier_by_kind": report.brier_by_kind(rows), "n": roi["n"],
        "arb_profit_cents": sum(report._profit(r) for r in arbs),
    }


class BalanceChart(PlotextPlot):
    """Braille line of the bankroll in dollars; deposits are '+', withdrawals '-'."""

    status = ""
    line_color = "profit"
    ticks: tuple[list[float], list[str]] = ([], [])

    def update_series(self, history: list[tuple[datetime, int]],
                      markers: list[tuple[datetime, int, str]]) -> None:
        plt = self.plt
        plt.clear_figure()
        plt.canvas_color(_SURFACE_RGB)
        plt.axes_color(_SURFACE_RGB)
        plt.ticks_color((148, 163, 184))
        if len(history) < 2:  # one point is not a line
            self.status = "No bankroll history yet"
            plt.title("No bankroll history yet - the chart starts after your first deposit and bet")
            self.refresh()
            return
        self.status = ""
        t0 = history[0][0]

        def hours(t: datetime) -> float:
            return (t - t0).total_seconds() / 3600

        self.line_color = "profit" if history[-1][1] >= history[0][1] else "loss"
        plt.plot([hours(t) for t, _ in history], [c / 100 for _, c in history],
                 marker="braille", color=_RGB[self.line_color])
        for kind, glyph in (("deposit", "+"), ("withdraw", "-")):
            pts = [(hours(t), c / 100) for t, c, k in markers if k == kind]
            if pts:
                plt.scatter([p[0] for p in pts], [p[1] for p in pts], marker=glyph,
                            color=(255, 255, 255))
        cents = [c for _, c in history] + [c for _, c, _ in markers]
        self.ticks = perf.dollar_ticks(min(cents), max(cents))
        plt.yticks(*self.ticks)
        plt.title("Bankroll over time")
        plt.xlabel("hours since first deposit")
        plt.ylabel("Bankroll")
        self.refresh()


class KpiTiles(Static):
    """Performance numbers in plain English; takes whatever KPI/report dict is available."""

    text = ""

    def update_kpis(self, k: dict) -> None:
        """Plain-English tiles; the luck-vs-skill verdict comes from the profit CI, not a bet count."""
        def pct(v, sign="+"):
            return "not enough data" if v is None else f"{v * 100:{sign}.1f}%"

        roi, (lo, hi) = k.get("roi"), k.get("roi_ci") or (None, None)
        profit = f"Return on money bet: {pct(roi)}"
        if roi is not None:
            c = round(abs(roi) * 100)
            profit += f" (about {c} cent{'s' * (c != 1)} {'made' if roi >= 0 else 'lost'} per $1)"
        if lo is not None and hi is not None:
            profit += f" (likely range {pct(lo)} to {pct(hi)})"
        clv = k.get("clv_mean")
        if clv is None:
            vs_close = "Our prices vs the closing price: not enough data"
        else:
            word = "better" if clv >= 0 else "worse"
            vs_close = (f"Our prices vs the closing price: {abs(clv) * 100:.1f}% {word} on average"
                        f" ({pct(k.get('clv_pos'), '')} of bets got a better price)")
        brier = k.get("brier")
        lines = [
            profit,
            vs_close,
            f"Won-lost-refunded: {k.get('record', '0-0-0')}",
            f"Biggest drop from a high point: ${k.get('max_dd', 0) / 100:,.2f}",
            "Accuracy of our chances: " + ("not enough data" if brier is None else
                                           f"{brier:.3f} (lower is better; 0.250 is a coin flip)"),
        ]
        n = k.get("n", 0)
        if k.get("conclusive") and lo is not None and lo > 0:
            lines.append(f"After {n} bets the profit looks like real skill, not luck")
        elif k.get("conclusive") and hi is not None and hi < 0:
            lines.append(f"After {n} bets we are losing by more than bad luck explains")
        else:
            lines.append(f"Too few bets to tell luck from skill yet ({n} settled)")
        lines += [str(x) for x in k.get("extra", []) or []]  # extra report lines (locked profit, vs market)
        cal = k.get("calibration") or []
        if cal:
            lines += ["How often our chances came true:", *(f"  {c}" for c in cal)]
        lines += [str(w) for w in k.get("warnings", []) or []]  # e.g. drift warnings from the report
        self.text = "\n".join(lines)
        self.update(Text(self.text))


class PerfMoney(Static):
    """Beside the chart: profit today / this week / all time, money deposited and withdrawn."""

    text = ""

    def update_money(self, windows: dict[str, int], deposited: int, withdrawn: int) -> None:
        lines = perf.money_lines(windows, deposited, withdrawn)
        self.text = "\n".join(lines)
        out = Text()
        for i, line in enumerate(lines):
            label, _, value = line.partition(": ")
            color = "" if not value.startswith(("+", "-")) else PROFIT if value.startswith("+") else LOSS
            out.append(("\n" if i else "") + label + ": ")
            out.append(value, style=f"bold {color}".strip())
        self.update(out)


class PerfTables(Static):
    """Results by bet type and the last settled bets."""

    text = ""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._args: tuple = ([], [], None, d.DEFAULT_NAMES)

    def update_tables(self, by_type: list[dict], recent: list, now: datetime,
                      names: d.Names = d.DEFAULT_NAMES) -> None:
        self._args = (by_type, recent, now, names)
        self._draw()

    def on_resize(self) -> None:
        if self._args[2] is not None:
            self._draw()

    def _draw(self) -> None:
        by_type, recent, now, names = self._args
        width = max(40, (self.size.width or 160) - 2)
        table = perf.type_table_lines(by_type)
        last = perf.recent_lines(recent, now, width, names) if now is not None else []
        out = Text()
        out.append("Results by bet type\n", style=f"bold {ACCENT}")
        for i, line in enumerate(table):
            out.append(line + "\n", style="bold" if i == 0 and len(table) > 1 else "")
        n = len(recent)
        out.append(f"\nLast {n} settled bet{'s' * (n != 1)}\n" if n else "\nLast settled bets\n",
                   style=f"bold {ACCENT}")
        for line in last:
            styled = Text(line)
            for word, color in ((" won ", PROFIT), (" lost ", LOSS)):
                at = line.find(word)
                if 0 <= at < 30:
                    styled.stylize(color, at, at + len(word))
            out.append_text(styled)
            out.append("\n")
        self.text = "\n".join(["Results by bet type", *table, "", "Last settled bets", *last])
        self.update(out)


class QuotaBar(Static):
    # free monthly allowances: Odds API credits, college-data calls, scores-service data points
    CAPS = {"Odds API": 500, "College data": 1000, "Scores data": 200000}
    UNITS = {"Odds API": "credits", "College data": "calls", "Scores data": "data points"}
    BAR_W = 8

    def __init__(self, **kw):
        super().__init__(**kw)
        self._pulsing = False

    def update_quota(self, odds: int | None, cfbd: int | None, rundown: int | None,
                     kalshi_ok: bool) -> None:
        out = Text()
        low = False
        for name, val in zip(self.CAPS, (odds, cfbd, rundown)):
            if val is None:
                out.append(f"{name}: not set up  ", style="dim")
                continue
            frac = max(0.0, min(1.0, val / self.CAPS[name]))
            is_low = frac < 0.10
            low = low or is_low
            filled = round(frac * self.BAR_W)
            out.append(f"{name} ")
            out.append("█" * filled + "░" * (self.BAR_W - filled),
                       style=LOSS if is_low else ACCENT)
            out.append(f" {val:,} {self.UNITS[name]} left this month  ")
        out.append("Kalshi: ")
        out.append("working" if kalshi_ok else "not reachable", style=PROFIT if kalshi_ok else LOSS)
        self.set_class(low, "low")
        self.update(out)
        if low and not self._pulsing:
            self._pulsing = True
            self._pulse()

    def _pulse(self) -> None:
        if not self.is_attached:
            self._pulsing = False
            return
        if not self.has_class("low"):
            self._pulsing = False
            self.styles.opacity = 1.0
            return
        target = 0.4 if self.styles.opacity > 0.7 else 1.0
        self.styles.animate("opacity", target, duration=0.8, on_complete=self._pulse)


class ScoresPanel(OptionList):
    """Scores tab: league and day headings, then one game each (score or kickoff on the first line;
    sportsbook lines, Kalshi win prices, our win chance and your bets under it). Live games blink."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.border_title = "Scores"
        self.rows: list[ScoreRow] = []
        self._live: list[int] = []
        self._lit = True

    def on_mount(self) -> None:
        self.set_interval(0.6, self._blink)

    def set_rows(self, rows: list[ScoreRow], delay_s: int | None = None) -> None:
        self.border_title = (f"Scores (about {round(delay_s / 60)} min behind)"
                             if delay_s is not None and delay_s >= 60 else "Scores")
        self.rows = list(rows)
        self._render()

    def on_resize(self) -> None:
        if self.rows:
            self._render()

    def prompt(self, row: ScoreRow, lit: bool = True) -> Text:
        if row.game is None:
            return Text(row.head, style="#94a3b8" if row.note else f"bold {ACCENT}")
        color = {"winning": PROFIT, "losing": LOSS}.get(row.bet_status, "")
        t = Text()
        t.append(row.head, style=f"bold {color}".strip())
        if row.state.startswith("LIVE"):
            t.append("  ")
            t.append("LIVE", style=f"bold {LOSS}" if lit else "dim")
            t.append(row.state[4:])
        else:
            t.append(f"  {row.state}", style="" if row.state.startswith("Final") else "#94a3b8")
        if row.details:
            append_packed(t, row.details, _list_width(self), style="#94a3b8")
        return t

    def _render(self) -> None:
        keep = self.highlighted
        self._live = [i for i, r in enumerate(self.rows) if r.game is not None and r.state.startswith("LIVE")]
        self.set_options(Option(self.prompt(r, self._lit), id=f"s{i}", disabled=r.game is None)
                         for i, r in enumerate(self.rows))
        if keep is not None and self.rows:
            self.highlighted = min(keep, len(self.rows) - 1)

    def _blink(self) -> None:
        self._lit = not self._lit
        for i in self._live:
            if i < self.option_count:
                self.replace_option_prompt_at_index(i, self.prompt(self.rows[i], self._lit))


class EventLog(RichLog):
    KIND_COLORS = {"rec": ACCENT, "text": "#a78bfa", "confirm": PROFIT, "deposit": PROFIT,
                   "withdraw": PENDING, "settle": "#60a5fa", "api": "#94a3b8",
                   "error": LOSS, "info": "#e2e8f0", "warn": PENDING}

    def __init__(self, **kw):
        super().__init__(wrap=True, markup=False, **kw)
        self.lines_text: list[str] = []
        self.last_kind, self.last_msg = "", ""

    LABELS = {"rec": "idea", "confirm": "placed", "settle": "settled", "api": "usage", "info": "note", "warn": "warning"}

    def event(self, kind: str, msg: str, at: datetime | None = None) -> None:
        """at: the app clock's time (wall clock if not given), shown as 12-hour PT."""
        line = Text(f"{d.clock(at or datetime.now(d.PT)):>8} ")
        line.append(f"{self.LABELS.get(kind, kind):<8}", style=self.KIND_COLORS.get(kind, "#e2e8f0"))
        line.append(f" {msg}")
        self.lines_text.append(line.plain)
        self.last_kind, self.last_msg = kind, msg
        self.write(line)


class BankrollHeader(Static):
    """One line: bankroll, today's change, money in open bets, Odds API credits left."""

    shown = reactive(0.0)  # cents, animated toward target_cents
    text = ""
    SHORTER = ((" this month", ""), (" of bankroll", ""), (" credits left", " left"), ("Odds API", "Odds"),
               (" in open bets", " in bets"), (" of ", "/"), (" left", ""))

    def __init__(self, **kw):
        super().__init__(**kw)
        self.target_cents = 0
        self.today_cents = 0
        self.open_cents: int | None = None
        self.odds: tuple[int | None, int] | None = None

    def on_mount(self) -> None:
        self._draw()

    def set_balance(self, cents: int, today_cents: int, open_cents: int | None = None,
                    odds: tuple[int | None, int] | None = None) -> None:
        self.target_cents = cents
        self.today_cents = today_cents
        self.open_cents = open_cents
        self.odds = odds
        self.animate("shown", float(cents), duration=0.6)
        self._draw()

    def watch_shown(self, _old: float, _new: float) -> None:
        self._draw()

    def _draw(self) -> None:
        arrow = "▲" if self.today_cents >= 0 else "▼"
        parts = [f"Bankroll {d.money(round(self.shown))}  {arrow} {d.money(abs(self.today_cents))} today"]
        if self.open_cents is not None:
            share = self.open_cents / self.target_cents if self.target_cents > 0 else 0.0
            parts.append(f"{d.money(self.open_cents)} in open bets ({share:.1%} of bankroll)")
        if self.odds is not None:
            left, cap = self.odds
            parts.append(f"Odds API: {'?' if left is None else f'{left:,}'} of {cap:,} credits left this month")
        self.text = " · ".join(parts)
        for long, short in self.SHORTER:  # narrow terminal: drop words until it fits on one line
            if not self.size.width or len(self.text) <= self.size.width - 2:
                break
            self.text = self.text.replace(long, short)
        out = Text(self.text)
        out.stylize(PROFIT if self.today_cents >= 0 else LOSS, self.text.index(arrow),
                    self.text.index(" today"))
        self.update(out)


SEP = " \u00b7 "
Piece = tuple[str, str]  # (text, style)


def append_packed(out: Text, segments: list, width: int, indent: str = "    ", style: str = "") -> None:
    """Start a new indented line on `out` and add the segments joined by ' · ', breaking only between
    segments (each wrapped line keeps the indent). A segment is a str or a list of (text, style) pieces."""
    cur = len(indent)
    out.append("\n" + indent)
    first = True
    for seg in segments:
        pieces: list[Piece] = [(seg, style)] if isinstance(seg, str) else list(seg)
        n = sum(len(t) for t, _ in pieces)
        if not n:
            continue
        if not first:
            if cur + len(SEP) + n > width:
                out.append("\n" + indent)
                cur = len(indent)
            else:
                out.append(SEP, style=style)
                cur += len(SEP)
        for t, st in pieces:
            out.append(t, style=st or style)
        cur += n
        first = False


def _list_width(w) -> int:
    """Usable text width inside an OptionList row (padding 0 1, room for the scrollbar)."""
    return max(30, (w.size.width or 160) - 4)


def place_now_segments(r: Recommendation, cells: list[str]) -> list:
    """The first detail line of a PLACE NOW row as packable segments; 'texted' rides with the time left so
    it never wraps onto a line by itself."""
    _, _, _, price, stake, profit, left, kick = cells
    profit_word = "locked profit" if r.kind == "arb" else "expected profit"
    time_left: list[Piece] = [(left, PENDING if "min left" in left and "hr" not in left else "")]
    if r.state == "PENDING":
        time_left.append((" (texted)", "dim"))
    return [price, f"bet {stake}", [(f"{profit_word} ", ""), (profit, PROFIT)], time_left, f"kickoff {kick} PT"]


class PlaceNowList(OptionList):
    """Recs that can be placed right now, two lines each; Enter confirm, s skip, d why."""

    BINDINGS = [Binding("s", "skip", "Skip"), Binding("d", "why", "Why this bet?"),
                Binding("enter", "select", "Confirm", show=True),
                Binding("e", "load_entry", "Open in YOUR ENTRY")]
    FLASH_S = 1.5

    class ConfirmRequested(Message):
        def __init__(self, rec_id: int):
            super().__init__()
            self.rec_id = rec_id

    class SkipRequested(ConfirmRequested):
        pass

    class WhyRequested(ConfirmRequested):
        pass

    class LoadEntryRequested(ConfirmRequested):
        pass

    def __init__(self, **kw):
        super().__init__(**kw)
        self.recs: list[Recommendation] = []
        self.details: dict[int, str] = {}
        self.flash_ids: set[int] = set()
        self._seen: set[int] = set()
        self._args: tuple = ()

    def set_recs(self, recs: list[Recommendation], now: datetime, games: dict,
                 names: d.Names = d.DEFAULT_NAMES, details: dict[int, str] | None = None) -> None:
        """details: rec id -> the extra line (chances, contracts, payout, source) from home.place_now_detail."""
        self.recs = list(recs)
        self.details = dict(details or {})
        self._args = (now, games, names)
        for r in recs:
            if r.id is not None and r.id not in self._seen:
                self._seen.add(r.id)
                self.flash_ids.add(r.id)
                self.set_timer(self.FLASH_S, lambda i=r.id: self._unflash(i))
        self._render()

    def on_resize(self) -> None:
        if self.recs:
            self._render()

    def _unflash(self, rec_id: int) -> None:
        self.flash_ids.discard(rec_id)
        self._render()

    def prompt(self, r: Recommendation, rank: int) -> Text:
        """Rank first (the list is best first), then where and the bet; the idea number ends the line below."""
        now, games, names = self._args
        rid, where, bet, price, stake, profit, left, kick = place_now_cells(r, now, games, names)
        out = Text()
        out.append(f"{rank}. ", style=f"bold {ACCENT}")
        out.append(where, style=f"bold {PROFIT}" if r.kind == "arb" else "bold")
        out.append(f"  {bet}")
        width = _list_width(self)
        segments = place_now_segments(r, [rid, where, bet, price, stake, profit, left, kick])
        detail = self.details.get(r.id)
        append_packed(out, segments if detail else [*segments, f"idea #{rid}"], width)
        if detail:
            append_packed(out, [*detail.split(SEP), f"idea #{rid}"], width, style="#94a3b8")
        if r.id in self.flash_ids:
            out.stylize(f"on {ACCENT}", 0, len(out))
            out.stylize("#0b0f1a")
        return out

    def _render(self) -> None:
        keep = self.highlighted_option.id if self.highlighted_option is not None else None
        self.set_options(Option(self.prompt(r, rank), id=str(r.id)) for rank, r in enumerate(self.recs, 1))
        if not self.recs:
            return
        ids = [str(r.id) for r in self.recs]
        self.highlighted = ids.index(keep) if keep in ids else 0

    def _current(self) -> int | None:
        opt = self.highlighted_option
        return None if opt is None or opt.id is None else int(opt.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.post_message(self.ConfirmRequested(int(event.option.id)))

    def action_skip(self) -> None:
        if (i := self._current()) is not None:
            self.post_message(self.SkipRequested(i))

    def action_why(self) -> None:
        if (i := self._current()) is not None:
            self.post_message(self.WhyRequested(i))

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "load_entry":  # only PrizePicks entries can be opened in YOUR ENTRY
            i = self._current()
            return any(r.id == i and r.kind == "pp_entry" for r in self.recs)
        return True

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        self.refresh_bindings()

    def action_load_entry(self) -> None:
        if (i := self._current()) is not None:
            self.post_message(self.LoadEntryRequested(i))


class PickList(OptionList):
    """A PrizePicks pick list (ALL LINES): game headings are disabled rows; Enter or
    space adds/removes the highlighted line in YOUR ENTRY, d explains it."""

    BINDINGS = [Binding("enter", "select", "Add/remove pick"), Binding("space", "select", "Add/remove pick",
                                                                       show=False),
                Binding("d", "why", "Why this pick?")]

    class ToggleRequested(Message):
        def __init__(self, key: PickKey):
            super().__init__()
            self.key = key

    class WhyRequested(ToggleRequested):
        pass

    class NothingToPick(Message):
        """Enter or space with no line that can be picked in this list."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rows: list[PickRow] = []

    def set_rows(self, rows: list[PickRow]) -> None:
        if rows == self.rows:
            return
        cur = self._current()
        keep = self.highlighted
        self.rows = list(rows)
        opts = []
        for i, r in enumerate(self.rows):
            t = Text(r.text)
            if r.heading:
                t.stylize(f"bold {ACCENT}")
            elif r.key is None:
                t.stylize("#64748b")
            elif r.chosen:
                t.stylize(f"bold {PROFIT}", 0, 4)
            opts.append(Option(t, id=f"r{i}", disabled=r.key is None))
        self.set_options(opts)
        keys = [r.key for r in self.rows]
        if cur is not None and cur in keys:
            self.highlighted = keys.index(cur)
        elif any(k is not None for k in keys):
            start = min(keep or 0, len(keys) - 1)
            nxt = [i for i in range(start, len(keys)) if keys[i] is not None] or \
                [i for i in range(len(keys)) if keys[i] is not None]
            self.highlighted = nxt[0]

    @property
    def selectable(self) -> bool:
        return any(r.key is not None for r in self.rows)

    def highlight_first(self) -> None:
        """Move the highlight onto a line you can pick when it is on none (or on a heading)."""
        if self._current() is None and self.selectable:
            self.highlighted = next(i for i, r in enumerate(self.rows) if r.key is not None)

    def on_focus(self) -> None:
        self.highlight_first()

    def action_select(self) -> None:
        if self._current() is None:  # first Enter lands on a line; with none to pick, say why
            if self.selectable:
                self.highlight_first()
            else:
                self.post_message(self.NothingToPick())
            return
        super().action_select()

    def _current(self) -> PickKey | None:
        i = self.highlighted
        return self.rows[i].key if i is not None and 0 <= i < len(self.rows) else None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if (k := self._current()) is not None:
            self.post_message(self.ToggleRequested(k))

    def action_why(self) -> None:
        if (k := self._current()) is not None:
            self.post_message(self.WhyRequested(k))


class PlayList(OptionList):
    """SUGGESTED PLAYS on Home, best first: Enter opens the highlighted play in YOUR ENTRY."""

    BINDINGS = [Binding("enter", "select", "Open this play"), Binding("space", "select", "Open this play",
                                                                      show=False)]

    class LoadRequested(Message):
        def __init__(self, index: int):
            super().__init__()
            self.index = index

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rows: list[str] = []

    def set_rows(self, rows: list[str]) -> None:
        if rows == self.rows:
            return
        keep = self.highlighted
        self.rows = list(rows)
        opts = []
        for r in self.rows:
            t = Text(r)
            t.stylize(f"bold {PROFIT}", 0, r.index(".") + 1)
            opts.append(Option(t))
        self.set_options(opts)
        if self.rows:
            self.highlighted = min(keep or 0, len(self.rows) - 1)

    @property
    def selectable(self) -> bool:
        return bool(self.rows)

    def highlight_first(self) -> None:
        if self.highlighted is None and self.rows:
            self.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.post_message(self.LoadRequested(event.option_index))


class OpenBetsList(OptionList):
    views: list[OpenBetView] = []

    def set_bets(self, views: list[OpenBetView]) -> None:
        self.views = list(views)
        self._render()

    def on_resize(self) -> None:
        if self.views:
            self._render()

    def _render(self) -> None:
        width = _list_width(self)
        opts = []
        for v in self.views:
            t = Text()
            t.append(f"#{v.bet_id} ", style=f"bold {ACCENT}")
            t.append(v.text)
            append_packed(t, [x for x in (v.pays or f"{v.stake} in", v.game) if x and x != "-"], width)
            color = "" if v.pnl_cents is None else PROFIT if v.pnl_cents >= 0 else LOSS
            picks = v.pnl_cents is None and any(w in v.worth for w in ("hit", "missed", "pushed", "pending"))
            worth = [(x, color) for x in v.worth.split(SEP)] if v.pnl_cents is not None else \
                [("picks: " if picks else "") + v.worth]
            segs: list = [v.paid_text, *([[w] for w in worth] if v.pnl_cents is not None else worth),
                          *v.chances.split(SEP), "" if v.close == "not yet" else v.close]
            append_packed(t, [x for x in segs if x], width)
            if v.picks:
                append_packed(t, v.picks.split(SEP), width, style="#94a3b8")
            opts.append(Option(t, id=str(v.bet_id)))
        keep = self.highlighted
        self.set_options(opts)
        if keep is not None and opts:
            self.highlighted = min(keep, len(opts) - 1)


class WatchList(OptionList):
    """Watch-only ideas (shown, not texted), two lines each; Enter or a click records one you placed
    anyway, d explains it."""

    BINDINGS = [Binding("enter", "select", "I placed this"), Binding("d", "why", "Why this bet?")]

    class ConfirmRequested(Message):
        def __init__(self, rec_id: int):
            super().__init__()
            self.rec_id = rec_id

    class WhyRequested(ConfirmRequested):
        pass

    rows: list[tuple[int, str, str]] = []

    def set_rows(self, rows: list[tuple[int, str, str]]) -> None:
        """rows: (idea id, headline, detail) from home.watch_cells."""
        self.rows = list(rows)
        self._render()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.post_message(self.ConfirmRequested(int(event.option.id)))

    def action_why(self) -> None:
        opt = self.highlighted_option
        if opt is not None and opt.id is not None:
            self.post_message(self.WhyRequested(int(opt.id)))

    def on_resize(self) -> None:
        if self.rows:
            self._render()

    def _render(self) -> None:
        width = _list_width(self)
        keep = self.highlighted_option.id if self.highlighted_option is not None else None
        opts = []
        for rec_id, head, detail in self.rows:
            t = Text()
            num, _, rest = head.partition(" ")
            t.append(f"{num} ", style=f"bold {ACCENT}")
            t.append(rest)
            append_packed(t, detail.strip().split(SEP), width, style="#94a3b8")
            opts.append(Option(t, id=str(rec_id)))
        self.set_options(opts)
        ids = [o.id for o in opts]
        if ids:
            self.highlighted = ids.index(keep) if keep in ids else 0


class Packed(Static):
    """Short segments joined by ' · ', wrapped only between segments."""

    text = ""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.segments: list[str] = []

    def set_segments(self, segments: list[str]) -> None:
        self.segments = list(segments)
        self._draw()

    def on_resize(self) -> None:
        self._draw()

    def _draw(self) -> None:
        width = max(20, (self.size.width or 160) - 2)
        self.text = "\n".join(pack(self.segments, width))
        self.update(Text(self.text))


class InfoLine(Packed):
    """Under the bankroll: today's record, daily-limit room, next price check, how old the prices are."""


class Section(Vertical):
    """A titled box holding a list and the plain-English message shown when the list is empty."""

    def __init__(self, title: str, body, empty_id: str, summary_id: str | None = None, **kw):
        super().__init__(**kw)
        self.border_title = title
        self.body = body
        self.empty = Static("", id=empty_id, classes="empty")
        self.summary = Packed(id=summary_id, classes="summary") if summary_id else None

    def compose(self) -> ComposeResult:
        yield self.empty
        yield self.body
        if self.summary is not None:
            yield self.summary

    def show_summary(self, line: str | list[str] | None) -> None:
        """A short summary under the list, as a line or ' · ' segments (hidden when None)."""
        if self.summary is None:
            return
        segs = [] if line is None else line.split(SEP) if isinstance(line, str) else list(line)
        self.summary.set_segments(segs)
        self.summary.display = line is not None

    def show_empty(self, message: str | None) -> None:
        """message None: show the list; otherwise hide it and explain why it is empty."""
        self.message = message
        self.empty.update(Text(message or ""))
        self.empty.display = message is not None
        self.body.display = message is None
        self.set_class(message is not None, "is-empty")
