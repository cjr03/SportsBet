from pathlib import Path
from datetime import datetime, timedelta, timezone

from textual.app import App
from textual.widgets import DataTable, Input, Label

from gridiron_edge.types import Game, Leg, Recommendation
from gridiron_edge.ui.screens import ConfirmScreen
from gridiron_edge.ui.widgets import (
    BalanceChart, BankrollHeader, EventLog, KpiTiles, QuotaBar,
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


class ThemedHarness(Harness):
    CSS_PATH = THEME


async def test_confirm_modal_width_with_theme():
    app = ThemedHarness()
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(ConfirmScreen(rec(5)))
        await pilot.pause()
        assert app.screen.query_one("#confirm-box").region.width == 60


async def test_kpi_inconclusive():
    rows = [{"state": "SETTLED", "result": "won", "stake_cents": 100, "payout_cents": 200,
             "clv": 0.02, "p_win": 0.6}]
    k = compute_kpis(rows, [(T0, 10000), (T0 + timedelta(days=1), 9000)])
    assert k["n"] == 1 and k["record"] == "1-0-0" and k["max_dd"] == 0  # a deposit swing is not a drawdown
    assert k["roi"] == 1.0 and k["clv_pos"] == 1.0
    assert abs(k["brier"] - 0.16) < 1e-9
    app = Harness(KpiTiles())
    async with app.run_test():
        tiles = app.query_one(KpiTiles)
        tiles.update_kpis(k)
        assert "too few bets to tell luck from skill yet" in tiles.text.lower()
        for jargon in ("ROI", "CLV", "Max DD", "Brier", "inconclusive", "n<", "±"):
            assert jargon not in tiles.text
        assert tiles.text.isascii()


async def test_kpi_verdict_comes_from_ci_not_bet_count():
    app = Harness(KpiTiles())
    base = {"roi": 0.08, "roi_band": 0.05, "n": 300, "record": "180-120-0", "max_dd": 0,
            "clv_mean": 0.01, "clv_pos": 0.6, "brier": 0.2}
    async with app.run_test():
        tiles = app.query_one(KpiTiles)
        tiles.update_kpis({**base, "conclusive": True, "roi_ci": (0.03, 0.13)})
        assert "skill" in tiles.text and "too few" not in tiles.text.lower()
        assert "+3.0% to +13.0%" in tiles.text
        tiles.update_kpis({**base, "roi": -0.08, "conclusive": True, "roi_ci": (-0.13, -0.03)})
        assert "losing" in tiles.text.lower() and "too few" not in tiles.text.lower()
        tiles.update_kpis({**base, "n": 5000, "conclusive": False, "roi_ci": (-0.02, 0.18)})
        assert "too few bets to tell luck from skill yet" in tiles.text.lower()
        tiles.update_kpis({**base, "warnings": ["Recent bets are doing worse than the backtest."]})
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
        assert app.result == (0.47, 30, None)


async def test_confirm_modal_prizepicks_has_no_price_or_contracts():
    leg = Leg(venue="prizepicks", description="x", side="over", price=0.0, p_fair=0.6, game_id="g",
              player="Jalen Hurts", stat="pass_tds", line=1.5, team="PHI")
    pp = Recommendation(id=42, kind="pp_entry", legs=[leg] * 5, stake_cents=1000, qty=None, ev=0.09,
                        p_win=0.3, structure="5-flex", snapshot_ts=T0, expires_at=T0 + timedelta(hours=1))
    app = Harness()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen(pp), lambda r: setattr(app, "result", r))
        await pilot.pause(0.3)
        assert not app.screen.query("#price") and not app.screen.query("#qty")
        title = str(app.screen.query_one("#confirm-title").render())
        assert title == "Did you place PrizePicks entry #42 ($10.00, 5-pick Flex Play)?"
        assert app.screen.query_one("#stake", Input).value == "10.00"
        await pilot.click("#ok")
        await pilot.pause(0.3)
        assert app.result == (None, None, 1000)


async def test_confirm_modal_prizepicks_stake_is_editable_and_checked():
    leg = Leg(venue="prizepicks", description="x", side="over", price=0.0, p_fair=0.6, game_id="g",
              player="Jalen Hurts", stat="pass_tds", line=1.5, team="PHI")
    pp = Recommendation(id=None, kind="pp_entry", legs=[leg] * 3, stake_cents=0, qty=None, ev=-0.02,
                        p_win=0.2, structure="3-power", snapshot_ts=T0, expires_at=T0 + timedelta(hours=1))
    app = Harness()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen(pp), lambda r: setattr(app, "result", r))
        await pilot.pause(0.3)
        title = str(app.screen.query_one("#confirm-title").render())
        assert title == "Did you place this PrizePicks entry (3-pick Power Play)?"
        await pilot.click("#ok")  # no suggested amount: you must type one
        await pilot.pause(0.3)
        assert app.result == "unset" and app.screen.query_one("#error").display
        for bad in ("abc", "0"):
            app.screen.query_one("#stake", Input).value = bad
            await pilot.click("#ok")
            await pilot.pause(0.3)
            assert app.result == "unset"
        app.screen.query_one("#stake", Input).value = "$12.50"
        await pilot.click("#ok")
        await pilot.pause(0.3)
        assert app.result == (None, None, 1250)


async def test_confirm_modal_prizepicks_with_old_prices_suggests_no_amount():
    leg = Leg(venue="prizepicks", description="x", side="over", price=0.0, p_fair=0.6, game_id="g",
              player="Jalen Hurts", stat="pass_tds", line=1.5, team="PHI")
    pp = Recommendation(id=None, kind="pp_entry", legs=[leg] * 3, stake_cents=1000, qty=None, ev=0.05,
                        p_win=0.2, structure="3-power", snapshot_ts=T0, expires_at=T0 + timedelta(hours=1))
    app = Harness()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen(pp, warning="Prices are 7 hours old and may have moved."),
                        lambda r: setattr(app, "result", r))
        await pilot.pause(0.3)
        title = str(app.screen.query_one("#confirm-title").render())
        assert title == "Did you place this PrizePicks entry (3-pick Power Play)?"
        assert app.screen.query_one("#stake", Input).value == ""
        await pilot.click("#ok")  # old prices: you must type the amount
        await pilot.pause(0.3)
        assert app.result == "unset" and app.screen.query_one("#error").display
        app.screen.query_one("#stake", Input).value = "7"
        await pilot.click("#ok")
        await pilot.pause(0.3)
        assert app.result == (None, None, 700)


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


async def test_scores_panel_rows_headings_and_blink():
    from gridiron_edge.ui.scores import score_rows
    g = Game("g1", "nfl", 2026, 7, T0, "KC", "BUF", status="live", home_pts=14, away_pts=10,
             period="Q2", clock="3:00")
    later = Game("g2", "nfl", 2026, 7, T0 + timedelta(hours=3), "PHI", "DAL")
    rows = score_rows([g, later], T0, bet_counts={"g2": 1}, bet_status={"g1": (1, 0)})
    app = Harness(ScoresPanel())
    async with app.run_test(size=(100, 30)) as pilot:
        p = app.query_one(ScoresPanel)
        p.set_rows(rows, 300)
        await pilot.pause()
        assert p.border_title == "Scores (about 5 min behind)"
        assert p.option_count == 3 and p.get_option_at_index(0).disabled
        live = str(p.get_option_at_index(1).prompt)
        assert live.startswith("Bills at Chiefs  LIVE Bills 10, Chiefs 14") and "2nd quarter, 3:00 left" in live
        assert "your bet is winning right now" in live
        assert "your bets: 1" in str(p.get_option_at_index(2).prompt)
        await pilot.pause(0.7)  # blinked: the LIVE word dims, the text stays
        assert str(p.get_option_at_index(1).prompt) == live
        p.set_rows(rows, None)
        assert p.border_title == "Scores"


async def test_perf_money_and_tables():
    from gridiron_edge.ui.widgets import PerfMoney, PerfTables

    class Two(App):
        def compose(self):
            yield PerfMoney()
            yield PerfTables()

    app = Two()
    async with app.run_test(size=(100, 40)) as pilot:
        m = app.query_one(PerfMoney)
        m.update_money({"today": 420, "week": -310, "all": 0}, 50_000, 1_000)
        assert m.text.splitlines()[0] == "Profit today: +$4.20" and "Money withdrawn: $10.00" in m.text
        t = app.query_one(PerfTables)
        t.update_tables([], [], T0)
        await pilot.pause()
        assert "No settled bets yet" in t.text and "Last settled bets" in t.text


async def test_chart_empty_history_ok():
    app = Harness(BalanceChart())
    async with app.run_test() as pilot:
        c = app.query_one(BalanceChart)
        c.update_series([], [])
        await pilot.pause()
        assert c.status == "No bankroll history yet"
        c.update_series([(T0, 10000)], [])  # one point is not a line yet
        assert c.status == "No bankroll history yet"
        hist = [(T0, 10000), (T0 + timedelta(days=1), 12000)]
        c.update_series(hist, [(T0, 10000, "deposit")])
        assert c.status == "" and c.line_color == "profit"
        c.update_series([(T0, 12000), (T0 + timedelta(days=1), 10000)], [])
        assert c.line_color == "loss"


async def test_event_log_records_kind():
    app = Harness(EventLog())
    async with app.run_test() as pilot:
        log = app.query_one(EventLog)
        log.event("deposit", "added $100.00", T0)  # 17:00 UTC = 10:00 AM PT
        log.event("info", "later", T0 + timedelta(hours=3, minutes=28))
        await pilot.pause()
        assert len(log.lines_text) == 2
        line = log.lines_text[0]
        assert "deposit" in line and "added $100.00" in line
        assert line.startswith("10:00 AM ") and log.lines_text[1].startswith(" 1:28 PM ")


async def test_quota_low_class_and_header():
    app = Harness(QuotaBar())
    async with app.run_test() as pilot:
        q = app.query_one(QuotaBar)
        q.update_quota(400, 900, 150000, True)
        assert not q.has_class("low")
        shown = str(q.render())
        assert "150,000 data points left this month" in shown and "400 credits left this month" in shown
        assert "Kalshi: working" in shown
        q.update_quota(20, 900, None, False)
        assert q.has_class("low")
        await pilot.pause(0.5)
        assert q.styles.opacity < 1.0
        q.update_quota(400, 900, 150000, True)
        assert not q.has_class("low")
        await pilot.pause(1.0)
        assert q.styles.opacity == 1.0
    app = Harness(BankrollHeader())
    async with app.run_test(size=(140, 24)) as pilot:
        h = app.query_one(BankrollHeader)
        h.set_balance(123456, -500)
        assert h.target_cents == 123456
        await pilot.pause(0.9)
        assert h.text == "Bankroll $1,234.56  \u25bc $5.00 today"
        h.set_balance(128450, 4210, 9600, (412, 500))
        await pilot.pause(0.9)
        assert h.text == ("Bankroll $1,284.50  \u25b2 $42.10 today \u00b7 $96.00 in open bets "
                          "(7.5% of bankroll) \u00b7 Odds API: 412 of 500 credits left this month")
    app = Harness(BankrollHeader())
    async with app.run_test(size=(80, 24)) as pilot:  # narrow: drops the extra words, still one line
        h = app.query_one(BankrollHeader)
        h.set_balance(128450, 4210, 9600, (412, 500))
        await pilot.pause(0.9)
        assert h.text.endswith("(7.5%) \u00b7 Odds: 412/500") and len(h.text) <= 78




def test_append_packed_wraps_between_segments_with_the_indent():
    from rich.text import Text

    from gridiron_edge.ui.widgets import append_packed
    t = Text("head")
    append_packed(t, ["our chance 52%", [("pays ", ""), ("$40.00", "green")], "from Pinnacle"], 36)
    assert t.plain == "head\n    our chance 52% · pays $40.00\n    from Pinnacle"
    assert all(len(line) <= 36 for line in t.plain.splitlines())


def test_place_now_texted_stays_with_time_left_when_wrapped():
    from dataclasses import replace

    from rich.text import Text

    from gridiron_edge.ui.widgets import append_packed, place_now_segments
    cells = ["5", "Kalshi", "KC ML", "47 cents", "$14.10", "$0.85", "59 min left", "Sun 10:00 AM"]
    segs = place_now_segments(replace(rec(5), state="PENDING"), cells)
    for width in range(30, 170):
        t = Text()
        append_packed(t, segs, width)
        lines = t.plain.split("\n")
        assert all(ln.strip() != "texted" for ln in lines), (width, lines)
        assert any("59 min left" in ln and "texted" in ln for ln in lines), (width, lines)
    assert "texted" not in Text().join(Text(str(x)) for x in place_now_segments(rec(5), cells)).plain
