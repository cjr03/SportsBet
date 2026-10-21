"""TUI widgets: chart, KPI tiles, quota bars, opportunity board, scores, event log, header."""
from datetime import datetime
from typing import Literal

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, RichLog, Static, TabbedContent, TabPane
from textual_plotext import PlotextPlot

from gridiron_edge.bets import report
from gridiron_edge.types import Game, Recommendation

PROFIT, LOSS, PENDING, ACCENT = "#22c55e", "#ef4444", "#f59e0b", "#22d3ee"
_RGB = {"profit": (34, 197, 94), "loss": (239, 68, 68)}
_SURFACE_RGB = (18, 24, 41)
MIN_CONCLUSIVE_N = 1500


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
        if not history:
            self.status = "no history yet"
            plt.title("no history yet")
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
        plt.xlabel("hours")
        plt.ylabel("$")
        self.refresh()


class KpiTiles(Static):
    text = ""

    def update_kpis(self, k: dict) -> None:
        def pct(v, sign="+"):
            return "n/a" if v is None else f"{v * 100:{sign}.1f}%"

        roi = pct(k.get("roi"))
        if k.get("roi_band") is not None and k.get("roi") is not None:
            roi += f" ±{k['roi_band'] * 100:.1f}%"
        brier = k.get("brier")
        lines = [
            f"ROI {roi}",
            f"CLV {pct(k.get('clv_mean'))}  ({pct(k.get('clv_pos'), '')} pos)",
            f"Record {k.get('record', '0-0-0')}",
            f"Max DD ${k.get('max_dd', 0) / 100:,.2f}",
            f"Brier {'n/a' if brier is None else f'{brier:.3f}'}",
        ]
        n = k.get("n", 0)
        if n < MIN_CONCLUSIVE_N:
            lines.append(f"inconclusive (n<{MIN_CONCLUSIVE_N})  n={n}")
        self.text = "\n".join(lines)
        self.update(Text(self.text))


class QuotaBar(Static):
    CAPS = {"Odds": 500, "CFBD": 1000, "Rundown": 200000}
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
                out.append(f"{name} n/a  ", style="dim")
                continue
            frac = max(0.0, min(1.0, val / self.CAPS[name]))
            is_low = frac < 0.10
            low = low or is_low
            filled = round(frac * self.BAR_W)
            out.append(f"{name} ")
            out.append("█" * filled + "░" * (self.BAR_W - filled),
                       style=LOSS if is_low else ACCENT)
            out.append(f" {val}  ")
        out.append("Kalshi ")
        out.append("ok" if kalshi_ok else "down", style=PROFIT if kalshi_ok else LOSS)
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


class OpportunityTable(DataTable):
    COMPONENT_CLASSES = {"flash"}

    def __init__(self, **kw):
        super().__init__(cursor_type="row", **kw)

    def on_mount(self) -> None:
        self.add_columns("id", "what", "price", "fair", "EV", "stake", "state")


class OpportunityBoard(TabbedContent):
    TABS = {"kalshi_game": "Kalshi Games", "kalshi_prop": "Kalshi Props",
            "pp_entry": "PrizePicks", "arb": "Arbitrage"}
    FLASH_S = 1.5

    class ConfirmRequested(Message):
        def __init__(self, rec_id: int):
            super().__init__()
            self.rec_id = rec_id

    def __init__(self, **kw):
        super().__init__(**kw)
        for kind, title in self.TABS.items():
            self.compose_add_child(
                TabPane(title, OpportunityTable(id=f"tab-{kind}"), id=f"pane-{kind}"))
        self.flash_ids: set[int] = set()
        self._seen: set[int] = set()
        self._recs: list[Recommendation] = []

    def set_recs(self, recs: list[Recommendation]) -> None:
        self._recs = list(recs)
        for r in recs:
            if r.id is not None and r.id not in self._seen:
                self._seen.add(r.id)
                self.flash_ids.add(r.id)
                self.set_timer(self.FLASH_S, lambda i=r.id: self._unflash(i))
        self._render_rows()

    def _unflash(self, rec_id: int) -> None:
        self.flash_ids.discard(rec_id)
        self._render_rows()

    def _render_rows(self) -> None:
        for kind in self.TABS:
            table = self.query_one(f"#tab-{kind}", OpportunityTable)
            flash = table.get_component_rich_style("flash")
            row = table.cursor_row
            table.clear()
            for r in (x for x in self._recs if x.kind == kind):
                cells = [
                    str(r.id),
                    " + ".join(leg.description for leg in r.legs),
                    "/".join(f"{leg.price:.2f}" for leg in r.legs),
                    "/".join(f"{leg.p_fair:.2f}" for leg in r.legs),
                    f"{r.ev * 100:+.1f}%",
                    f"${r.stake_cents / 100:,.2f}",
                    r.state,
                ]
                if r.id in self.flash_ids:
                    styled = [Text(c, style=flash) for c in cells]
                else:
                    styled = [Text(c, style=PENDING if i == 6 and r.state == "PENDING" else "")
                              for i, c in enumerate(cells)]
                table.add_row(*styled, key=str(r.id))
            if table.row_count:
                table.move_cursor(row=min(row, table.row_count - 1))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if event.row_key.value is not None:
            self.post_message(self.ConfirmRequested(int(event.row_key.value)))


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
                  delay_s: int | None) -> None:
        self.border_title = (f"Scores ~{round(delay_s / 60)}m delay"
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
            else:
                state = Text(g.status)
            self.add_row(Text(f"{g.away} @ {g.home}", style=style), Text(score, style=style),
                         state, key=g.game_id)
        self._games = {g.game_id: g for g in games}

    @staticmethod
    def _live_cell(g: Game, lit: bool) -> Text:
        cell = Text("LIVE", style=f"bold {LOSS}" if lit else "dim")
        if g.period:
            cell.append(f" {g.period} {g.clock or ''}".rstrip())
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

    def event(self, kind: str, msg: str) -> None:
        line = Text(datetime.now().strftime("%H:%M:%S") + " ")
        line.append(f"{kind:<8}", style=self.KIND_COLORS.get(kind, "#e2e8f0"))
        line.append(f" {msg}")
        self.lines_text.append(line.plain)
        self.write(line)


class BankrollHeader(Static):
    shown = reactive(0.0)  # cents, animated toward target_cents
    text = ""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.target_cents = 0
        self.today_cents = 0

    def on_mount(self) -> None:
        self.set_interval(1.0, self._draw)
        self._draw()

    def set_balance(self, cents: int, today_cents: int) -> None:
        self.target_cents = cents
        self.today_cents = today_cents
        self.animate("shown", float(cents), duration=0.6)

    def watch_shown(self, _old: float, _new: float) -> None:
        self._draw()

    def _draw(self) -> None:
        sign = "+" if self.today_cents >= 0 else "-"
        self.text = (f"GRIDIRON EDGE  ${self.shown / 100:,.2f}  "
                     f"{sign}${abs(self.today_cents) / 100:,.2f} today  "
                     f"{datetime.now():%H:%M:%S}")
        self.update(Text(self.text))
