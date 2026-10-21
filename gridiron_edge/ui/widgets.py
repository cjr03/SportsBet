"""TUI widgets: header, PLACE NOW / OPEN BETS lists, chart, KPI tiles, quota bars, scores, event log."""
import math
from datetime import datetime
from typing import Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, OptionList, RichLog, Static
from textual.widgets.option_list import Option
from textual_plotext import PlotextPlot

from gridiron_edge.bets import report
from gridiron_edge import describe as d
from gridiron_edge.types import Game, Recommendation
from gridiron_edge.ui.home import OpenBetView, place_now_cells

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
        plt.title("Bankroll over time")
        plt.xlabel("hours since first deposit")
        plt.ylabel("Bankroll ($)")
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
        out.append("Kalshi ")
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


class ScoresPanel(DataTable):
    def __init__(self, **kw):
        super().__init__(cursor_type="row", **kw)
        self.border_title = "Scores"
        self._live_keys: list[str] = []
        self._lit = True

    def on_mount(self) -> None:
        self.add_columns("Game", "Score", "State")
        self.set_interval(0.6, self._blink)

    def set_games(self, games: list[Game],
                  bet_status: dict[str, Literal["winning", "losing", "none"]],
                  delay_s: int | None, names: d.Names = d.DEFAULT_NAMES) -> None:
        self.border_title = (f"Scores (about {round(delay_s / 60)} min behind)"
                             if delay_s is not None and delay_s >= 60 else "Scores")
        self.clear()
        self._live_keys = []
        colors = {"winning": PROFIT, "losing": LOSS}
        for g in games:
            style = colors.get(bet_status.get(g.game_id, "none"), "")
            score = ("-" if g.home_pts is None or g.away_pts is None
                     else f"{g.away_pts}-{g.home_pts}")
            if g.status == "live":
                state = self._live_cell(g, True)
                self._live_keys.append(g.game_id)
            elif g.status == "scheduled":
                state = Text(f"{g.kickoff.astimezone(d.PT):%a} {d.clock(g.kickoff)} PT")
            else:
                state = Text(g.status.replace("_", " ").capitalize())
            matchup = f"{names.team(g.league, g.away)} at {names.team(g.league, g.home)}"
            self.add_row(Text(matchup, style=style), Text(score, style=style),
                         state, key=g.game_id)
        self._games = {g.game_id: g for g in games}

    @staticmethod
    def _live_cell(g: Game, lit: bool) -> Text:
        cell = Text("LIVE", style=f"bold {LOSS}" if lit else "dim")
        if g.period:
            cell.append(f" {d.period_text(g.period, g.clock, short=True)}")
        return cell

    def _blink(self) -> None:
        self._lit = not self._lit
        col = self.ordered_columns[2].key
        for key in self._live_keys:
            self.update_cell(key, col, self._live_cell(self._games[key], self._lit))


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


class PlaceNowList(OptionList):
    """Recs that can be placed right now, two lines each; Enter confirm, s skip, d why."""

    BINDINGS = [Binding("s", "skip", "Skip"), Binding("d", "why", "Why this bet?"),
                Binding("enter", "select", "Confirm", show=True)]
    FLASH_S = 1.5

    class ConfirmRequested(Message):
        def __init__(self, rec_id: int):
            super().__init__()
            self.rec_id = rec_id

    class SkipRequested(ConfirmRequested):
        pass

    class WhyRequested(ConfirmRequested):
        pass

    def __init__(self, **kw):
        super().__init__(**kw)
        self.recs: list[Recommendation] = []
        self.flash_ids: set[int] = set()
        self._seen: set[int] = set()
        self._args: tuple = ()

    def set_recs(self, recs: list[Recommendation], now: datetime, games: dict,
                 names: d.Names = d.DEFAULT_NAMES) -> None:
        self.recs = list(recs)
        self._args = (now, games, names)
        for r in recs:
            if r.id is not None and r.id not in self._seen:
                self._seen.add(r.id)
                self.flash_ids.add(r.id)
                self.set_timer(self.FLASH_S, lambda i=r.id: self._unflash(i))
        self._render()

    def _unflash(self, rec_id: int) -> None:
        self.flash_ids.discard(rec_id)
        self._render()

    def prompt(self, r: Recommendation) -> Text:
        now, games, names = self._args
        rid, where, bet, price, stake, profit, left, kick = place_now_cells(r, now, games, names)
        out = Text()
        out.append(f"#{rid} ", style=f"bold {ACCENT}")
        out.append(where, style=f"bold {PROFIT}" if r.kind == "arb" else "bold")
        out.append(f"  {bet}\n")
        profit_word = "locked profit" if r.kind == "arb" else "expected profit"
        out.append(f"    {price} \u00b7 bet {stake} \u00b7 {profit_word} ")
        out.append(profit, style=PROFIT)
        out.append(f" \u00b7 {left}", style=PENDING if "min left" in left and "hr" not in left else "")
        out.append(f" \u00b7 kickoff {kick}")
        if r.state == "PENDING":
            out.append("  (texted)", style="dim")
        if r.id in self.flash_ids:
            out.stylize(f"on {ACCENT}", 0, len(out))
            out.stylize("#0b0f1a")
        return out

    def _render(self) -> None:
        keep = self.highlighted_option.id if self.highlighted_option is not None else None
        self.set_options(Option(self.prompt(r), id=str(r.id)) for r in self.recs)
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


class OpenBetsList(OptionList):
    def set_bets(self, views: list[OpenBetView]) -> None:
        self.views = list(views)
        opts = []
        for v in views:
            t = Text()
            t.append(f"#{v.bet_id} ", style=f"bold {ACCENT}")
            t.append(f"{v.text}\n")
            t.append(f"    {v.stake} in \u00b7 {v.game} \u00b7 ")
            color = "" if v.pnl_cents is None else PROFIT if v.pnl_cents >= 0 else LOSS
            picks = v.pnl_cents is None and any(w in v.worth for w in ("hit", "missed", "pushed", "pending"))
            t.append("picks: " if picks else "worth now ")
            t.append(v.worth, style=color)
            if v.close != "not yet":
                t.append(f" \u00b7 {v.close}")
            opts.append(Option(t, id=str(v.bet_id)))
        self.set_options(opts)


class Section(Vertical):
    """A titled box holding a list and the plain-English message shown when the list is empty."""

    def __init__(self, title: str, body, empty_id: str, **kw):
        super().__init__(**kw)
        self.border_title = title
        self.body = body
        self.empty = Static("", id=empty_id, classes="empty")

    def compose(self) -> ComposeResult:
        yield self.empty
        yield self.body

    def show_empty(self, message: str | None) -> None:
        """message None: show the list; otherwise hide it and explain why it is empty."""
        self.message = message
        self.empty.update(Text(message or ""))
        self.empty.display = message is not None
        self.body.display = message is None
