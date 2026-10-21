from pathlib import Path
from datetime import datetime, timedelta, timezone

from textual.app import App
from textual.widgets import DataTable, Input

from gridiron_edge.types import Game, Leg, Recommendation
from gridiron_edge.ui.screens import ConfirmScreen
from gridiron_edge.ui.widgets import (
    BalanceChart, BankrollHeader, EventLog, KpiTiles, OpportunityBoard, QuotaBar,
    ScoresPanel, compute_kpis,
)

THEME = Path(__file__).resolve().parents[2] / "gridiron_edge" / "ui" / "theme.tcss"

T0 = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)


def rec(rec_id, kind="kalshi_game", price=0.47, qty=30):
    leg = Leg(venue="kalshi", description="KC ML", side="yes", price=price, p_fair=0.55,
              game_id="g", ticker=f"T{rec_id}")
    return Recommendation(id=rec_id, kind=kind, legs=[leg], stake_cents=1410, qty=qty,
                          ev=0.06, p_win=0.55, structure=None, snapshot_ts=T0,
                          expires_at=T0 + timedelta(hours=1))


class Harness(App):
    def __init__(self, widget=None):
        super().__init__()
        self.widget = widget
        self.requested = []
        self.result = "unset"

    def compose(self):
        if self.widget is not None:
            yield self.widget

    def on_opportunity_board_confirm_requested(self, msg):
        self.requested.append(msg.rec_id)


class ThemedHarness(Harness):
    CSS_PATH = THEME


async def test_board_flash_styled_then_expires():
    app = ThemedHarness(OpportunityBoard())
    async with app.run_test() as pilot:
        board = app.query_one(OpportunityBoard)
        board.set_recs([rec(1)])
        await pilot.pause(board.FLASH_S + 0.3)
        board.set_recs([rec(1), rec(2)])
        await pilot.pause()
        table = board.query_one("#tab-kalshi_game", DataTable)

        assert 2 in board.flash_ids and 1 not in board.flash_ids
        assert table.get_component_rich_style("flash").bgcolor is not None
        flashed = str(table.get_row_at(1)[0].style)
        assert flashed and "on " in flashed
        assert "on " not in str(table.get_row_at(0)[0].style)
        await pilot.pause(board.FLASH_S + 0.3)
        assert board.flash_ids == set()
        assert "on " not in str(table.get_row_at(1)[0].style)


async def test_confirm_modal_width_with_theme():
    app = ThemedHarness()
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(ConfirmScreen(rec(5)))
        await pilot.pause()
        assert app.screen.query_one("#confirm-box").region.width == 60


async def test_board_flash_and_confirm_message():
    app = Harness(OpportunityBoard())
    async with app.run_test() as pilot:
        board = app.query_one(OpportunityBoard)
        board.set_recs([rec(1)])
        board.set_recs([rec(1), rec(2)])
        await pilot.pause()
        assert 2 in board.flash_ids
        table = board.query_one("#tab-kalshi_game", DataTable)
        assert table.row_count == 2
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.requested == [1]


async def test_board_routes_by_kind():
    app = Harness(OpportunityBoard())
    async with app.run_test() as pilot:
        board = app.query_one(OpportunityBoard)
        board.set_recs([rec(1, "kalshi_prop"), rec(2, "pp_entry"), rec(3, "arb")])
        await pilot.pause()
        for kind in ("kalshi_prop", "pp_entry", "arb"):
            assert board.query_one(f"#tab-{kind}", DataTable).row_count == 1
        assert board.query_one("#tab-kalshi_game", DataTable).row_count == 0


async def test_kpi_inconclusive():
    rows = [{"state": "SETTLED", "result": "won", "stake_cents": 100, "payout_cents": 200,
             "clv": 0.02, "p_win": 0.6}]
    k = compute_kpis(rows, [(T0, 10000), (T0 + timedelta(days=1), 9000)])
    assert k["n"] == 1 and k["record"] == "1-0-0" and k["max_dd"] == 1000
    assert k["roi"] == 1.0 and k["clv_pos"] == 1.0
    assert abs(k["brier"] - 0.16) < 1e-9
    app = Harness(KpiTiles())
    async with app.run_test():
        tiles = app.query_one(KpiTiles)
        tiles.update_kpis(k)
        assert "Too few bets to judge yet: 1 of the 1,500 needed" in tiles.text
        assert "Return on money bet: +100.0%" in tiles.text and "Accuracy score: 0.160" in tiles.text
        for word in ("ROI", "CLV", "Brier", "DD"):
            assert word not in tiles.text
        tiles.update_kpis({**k, "warnings": ["Recent bets are doing worse than the backtest."]})
        assert tiles.text.endswith("Recent bets are doing worse than the backtest.")


def test_kpi_empty():
    k = compute_kpis([], [])
    assert k["n"] == 0 and k["roi"] is None and k["max_dd"] == 0


async def test_confirm_modal_returns_values():
    app = Harness()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen(rec(5)), lambda r: setattr(app, "result", r))
        await pilot.pause(0.3)
        assert app.screen.query_one("#price", Input).value == "0.47"
        assert app.screen.query_one("#qty", Input).value == "30"
        for sel, text in (("#price", "0.47"), ("#qty", "30")):
            inp = app.screen.query_one(sel, Input)
            inp.value = ""
            inp.focus()
            await pilot.press(*text)
        await pilot.click("#ok")
        await pilot.pause(0.3)
        assert app.result == (0.47, 30)


async def test_confirm_modal_rejects_bad_price():
    app = Harness()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen(rec(5)), lambda r: setattr(app, "result", r))
        await pilot.pause(0.3)
        inp = app.screen.query_one("#price", Input)
        inp.value = "1.7"
        await pilot.click("#ok")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ConfirmScreen)
        assert app.result == "unset"
        assert app.screen.query_one("#error").display
        await pilot.click("#cancel")
        await pilot.pause(0.3)
        assert app.result is None


async def test_scores_delay_title():
    g = Game("g1", "nfl", 2026, 7, T0, "KC", "BUF", status="live", home_pts=14, away_pts=10,
             period="Q2", clock="3:00")
    app = Harness(ScoresPanel())
    async with app.run_test() as pilot:
        p = app.query_one(ScoresPanel)
        p.set_games([g], {"g1": "winning"}, 300)
        await pilot.pause()
        assert p.border_title == "Scores (about 5 min behind)"
        assert str(p.get_row_at(0)[0]) == "Bills at Chiefs"
        assert p.row_count == 1
        p.set_games([g], {}, None)
        assert p.border_title == "Scores"


async def test_chart_empty_history_ok():
    app = Harness(BalanceChart())
    async with app.run_test() as pilot:
        c = app.query_one(BalanceChart)
        c.update_series([], [])
        await pilot.pause()
        assert c.status == "no history yet"
        hist = [(T0, 10000), (T0 + timedelta(days=1), 12000)]
        c.update_series(hist, [(T0, 10000, "deposit")])
        assert c.status == "" and c.line_color == "profit"
        c.update_series([(T0, 12000), (T0 + timedelta(days=1), 10000)], [])
        assert c.line_color == "loss"


async def test_event_log_records_kind():
    app = Harness(EventLog())
    async with app.run_test() as pilot:
        log = app.query_one(EventLog)
        log.event("deposit", "added $100.00")
        await pilot.pause()
        assert len(log.lines_text) == 1
        line = log.lines_text[0]
        assert "deposit" in line and "added $100.00" in line
        assert line[2] == ":" and line[5] == ":"


async def test_quota_low_class_and_header():
    app = Harness(QuotaBar())
    async with app.run_test() as pilot:
        q = app.query_one(QuotaBar)
        q.update_quota(400, 900, 150000, True)
        assert not q.has_class("low")
        q.update_quota(20, 900, None, False)
        assert q.has_class("low")
        await pilot.pause(0.5)
        assert q.styles.opacity < 1.0
        q.update_quota(400, 900, 150000, True)
        assert not q.has_class("low")
        await pilot.pause(1.0)
        assert q.styles.opacity == 1.0
    app = Harness(BankrollHeader())
    async with app.run_test(size=(120, 24)) as pilot:
        h = app.query_one(BankrollHeader)
        h.set_balance(123456, -500)
        assert h.target_cents == 123456
        await pilot.pause(0.9)
        assert h.text == "Bankroll $1,234.56  \u25bc $5.00 today"
        h.set_balance(128450, 4210, 9600, (412, 500))
        await pilot.pause(0.9)
        assert h.text == ("Bankroll $1,284.50  \u25b2 $42.10 today \u00b7 $96.00 in open bets "
                          "(7.5% of bankroll) \u00b7 Odds API 412/500 left")
    app = Harness(BankrollHeader())
    async with app.run_test(size=(80, 24)) as pilot:  # narrow: drops the extra words, still one line
        h = app.query_one(BankrollHeader)
        h.set_balance(128450, 4210, 9600, (412, 500))
        await pilot.pause(0.9)
        assert h.text.endswith("in open bets (7.5%) \u00b7 Odds 412/500") and len(h.text) <= 78
