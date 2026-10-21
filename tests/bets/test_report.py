import json
import math

import pytest

from gridiron_edge.bets import report


def row(kind="kalshi_game", result="won", stake=100, fee=0, payout=200, settled_at="2026-10-25T20:00:00+00:00",
        clv=None, p_win=None, legs=None, structure=None):
    legs = legs if legs is not None else [{"game_id": "nfl:20261025:DAL@PHI", "team": "DAL", "line": None}]
    return {"kind": kind, "result": result, "stake_cents": stake, "fee_cents": fee, "payout_cents": payout,
            "settled_at": settled_at, "clv": clv, "p_win": p_win, "legs_json": json.dumps(legs),
            "structure": structure}


def test_roi_includes_kalshi_fees():
    s = report.roi_stats([row(stake=100, fee=4, payout=200), row(result="lost", stake=100, fee=4, payout=0)])
    # profit = 200 - 104 - 104 = -8 on 208 cost
    assert s["profit_cents"] == -8 and s["cost_cents"] == 208
    assert s["roi"] == pytest.approx(-8 / 208)


def test_roi_ci_is_delta_method_on_ratio():
    rows = [row(stake=100, payout=200), row(result="lost", stake=300, payout=0), row(stake=100, payout=250)]
    s = report.roi_stats(rows)
    S = [100, 300, 100]
    P = [100, -300, 150]
    R = sum(P) / sum(S)
    se = math.sqrt(3 / 2 * sum((p - R * c) ** 2 for p, c in zip(P, S))) / sum(S)
    assert s["roi"] == pytest.approx(R) and s["se"] == pytest.approx(se)
    assert s["ci_lo"] == pytest.approx(R - 1.96 * se) and s["ci_hi"] == pytest.approx(R + 1.96 * se)
    assert s["conclusive"] is False


def test_roi_conclusive_when_ci_excludes_zero():
    rows = [row(stake=100, payout=200 + i) for i in range(30)] + [row(result="lost", stake=100, payout=0)]
    assert report.roi_stats(rows)["conclusive"] is True


def test_drawdown_on_settled_trading_pnl_only():
    rows = [row(stake=100, fee=2, payout=0, result="lost", settled_at="2026-10-25T20:00:00+00:00"),
            row(stake=100, fee=2, payout=300, settled_at="2026-10-25T19:00:00+00:00"),
            row(stake=100, fee=2, payout=0, result="lost", settled_at="2026-10-25T21:00:00+00:00"),
            row(result="refund", stake=100, fee=2, payout=102, settled_at="2026-10-25T22:00:00+00:00")]
    dd = report.drawdown(rows)
    # ordered by settled_at: +198, -102, -102, 0 -> peak 198, trough -6
    assert dd["max_dd_cents"] == 204 and dd["current_dd_cents"] == 204
    assert dd["peak_cents"] == 198


def test_drawdown_pct_of_peak_equity():
    rows = [row(stake=100, payout=300), row(result="lost", stake=100, payout=0,
                                             settled_at="2026-10-26T20:00:00+00:00")]
    dd = report.drawdown(rows, starting_cents=1000)
    assert dd["max_dd_cents"] == 100 and dd["max_dd_pct"] == pytest.approx(100 / 1200)


def test_brier_pp_scores_profit_event_and_skips_arbs():
    legs = [{"game_id": "nfl:x", "player": "A"}]
    rows = [row(kind="pp_entry", result="won", stake=1000, payout=400, p_win=0.3, legs=legs, structure="5-flex"),
            row(kind="pp_entry", result="won", stake=1000, payout=3000, p_win=0.3, legs=legs, structure="5-flex"),
            row(kind="arb", result="won", stake=900, payout=1000, p_win=1.0),
            row(kind="kalshi_game", result="lost", stake=50, payout=0, p_win=0.6)]
    b = report.brier_by_kind(rows)
    assert "arb" not in b
    # 0.4x payout is a loss of the P(mult>1) event
    assert b["pp_entry"]["brier"] == pytest.approx(((0.3 - 0) ** 2 + (0.3 - 1) ** 2) / 2)
    assert b["kalshi_game"]["brier"] == pytest.approx(0.36)


def test_segment_names():
    assert report.segment(row()) == "nfl ml"
    assert report.segment(row(legs=[{"game_id": "cfb:1", "team": "UGA", "line": 3.5}])) == "cfb spread"
    assert report.segment(row(legs=[{"game_id": "nfl:1", "team": None, "line": 44.5}])) == "nfl total"
    assert report.segment(row(kind="kalshi_prop", legs=[{"game_id": "nfl:1", "player": "A"}])) == "nfl prop"
    assert report.segment(row(kind="pp_entry", legs=[{"game_id": "nfl:1"}])) == "nfl prizepicks"
    assert report.segment(row(kind="arb")) == "arb"


def test_clv_stats_mean_se_t():
    s = report.clv_stats([0.02, 0.04, -0.01, 0.03])
    mean = 0.02
    sd = math.sqrt(sum((x - mean) ** 2 for x in [0.02, 0.04, -0.01, 0.03]) / 3)
    assert s["mean"] == pytest.approx(mean) and s["se"] == pytest.approx(sd / 2)
    assert s["t"] == pytest.approx(mean / (sd / 2)) and s["pos_share"] == 0.75 and s["n"] == 4
    assert report.clv_stats([])["mean"] is None


def test_compute_kpis_uses_fee_and_settled_pnl():
    from gridiron_edge.ui.widgets import compute_kpis
    rows = [row(stake=100, fee=4, payout=200, p_win=0.6), row(kind="arb", stake=900, fee=10, payout=1000, p_win=1.0)]
    k = compute_kpis(rows, [])
    assert k["roi"] == pytest.approx(96 / 104)  # arbs are reported as profit, not in ROI
    assert k["arb_profit_cents"] == 90 and k["n"] == 1
    assert k["brier"] == pytest.approx(0.16)


# -- build_report over a database ----------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

from gridiron_edge.bets.lifecycle import Lifecycle  # noqa: E402
from gridiron_edge.bets.settle import Settler  # noqa: E402
from gridiron_edge.db import Ledger  # noqa: E402
from gridiron_edge.matching import GameRegistry, TeamResolver  # noqa: E402
from gridiron_edge.types import Leg, Recommendation  # noqa: E402

T0 = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)


def _db(conn):
    ledger = Ledger(conn)
    ledger.deposit(100_000)
    lc = Lifecycle(conn, ledger)
    st = Settler(conn, lc, GameRegistry(conn, TeamResolver(conn)))
    return ledger, lc, st


def _rec(i, line=3.5, team="PHI", observe=False, p_mkt=0.5, p_win=0.55, league="nfl"):
    leg = Leg("kalshi", "x", "yes", 0.50, p_win, f"{league}:2026102{i % 10}:A@B", ticker=f"T{i}", team=team,
              line=line, p_mkt=p_mkt)
    return Recommendation(None, "kalshi_game", [leg], 0 if observe else 500, 10, 0.05, p_win, None,
                          T0, T0 + timedelta(hours=3), observe_only=observe, p_mkt=p_mkt)


def test_build_report_shape_and_plain_english(conn):
    ledger, lc, st = _db(conn)
    for i in range(4):
        rec, _ = lc.upsert_recommendation(_rec(i), T0)
        bet = lc.confirm(rec.id, T0)
        st.record_clv(bet, 0.56)
        st.record_rec_clv(rec.id, 0.56, T0)
        st.manual_settle(bet, "won" if i % 2 else "lost", now=T0 + timedelta(hours=i + 4))
    obs, _ = lc.upsert_recommendation(_rec(9, observe=True), T0)
    st.record_rec_clv(obs.id, 0.45, T0)
    r = report.build_report(conn, T0 + timedelta(days=1))
    assert r["placed"]["roi"]["n"] == 4 and r["placed"]["record"] == "2-2-0"
    assert r["clv"]["placed"]["n"] == 4 and r["clv"]["all_signals"]["n"] == 5
    assert "nfl spread" in r["clv"]["by_segment"]
    assert r["calibration"]["brier"] is not None and r["calibration"]["brier_market"] is not None
    assert len(r["calibration"]["reliability"]) == 5
    assert r["drawdown"]["max_dd_cents"] >= 0
    text = " ".join(r["summary"])
    for jargon in ("CLV", "Brier", "EV", "ROI"):
        assert jargon not in text
    assert all(s.isascii() for s in r["summary"])


def test_drift_flags_segment_when_rolling_clv_significantly_negative(conn):
    _, lc, st = _db(conn)
    for i in range(60):
        rec, _ = lc.upsert_recommendation(_rec(i, observe=True), T0 + timedelta(minutes=i))
        st.record_rec_clv(rec.id, 0.47 + (i % 3) * 0.005, T0 + timedelta(minutes=i))
    d = report.drift(conn, window=50)
    seg = d["nfl spread"]
    assert seg["n"] == 50 and seg["hi"] < 0 and seg["alarm"] is True
    assert report.drift(conn, window=100)["nfl spread"]["alarm"] is False  # not enough signals yet


def test_drift_calibration_vs_backtest(conn):
    ledger, lc, st = _db(conn)
    for i in range(10):
        rec, _ = lc.upsert_recommendation(_rec(i, p_win=0.9), T0)
        bet = lc.confirm(rec.id, T0)
        st.manual_settle(bet, "lost", now=T0 + timedelta(hours=i))
    r = report.build_report(conn, T0 + timedelta(days=1), backtest={"nfl": {"brier": 0.24}})
    cal = r["drift"]["calibration"]
    assert cal["rolling_brier"] == pytest.approx(0.81) and cal["backtest_brier"] == 0.24 and cal["worse"]


def test_by_type_table_and_profit_windows():
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc)  # Wed 12:00 PM PT
    rows = [row(stake=100, fee=2, payout=200, settled_at="2026-10-28T18:00:00+00:00"),  # today: +98
            row(result="lost", stake=100, payout=0, settled_at="2026-10-26T18:00:00+00:00"),  # Mon: -100
            row(kind="pp_entry", result="refund", stake=500, payout=500, settled_at="2026-10-24T18:00:00+00:00"),
            row(kind="arb", stake=950, payout=1000, settled_at="2026-10-20T18:00:00+00:00"),  # +50
            {**row(result=None, payout=None), "settled_at": None}]  # still open: ignored
    t = report.by_type(rows)
    assert [x["label"] for x in t] == ["Kalshi game bets", "PrizePicks", "Locked profit"]
    game = t[0]
    assert (game["bets"], game["won"], game["lost"], game["refunded"]) == (2, 1, 1, 0)
    assert game["cost_cents"] == 202 and game["profit_cents"] == -2 and game["roi"] == pytest.approx(-2 / 202)
    assert t[1]["refunded"] == 1 and t[1]["profit_cents"] == 0
    assert report.profit_windows(rows, now, pt) == {"today": 98, "week": -2, "all": 48}
    assert [r["settled_at"][:10] for r in report.recent_settled(rows, 2)] == ["2026-10-28", "2026-10-26"]


def test_money_moves(conn):
    from gridiron_edge.db import Ledger
    ledger = Ledger(conn)
    ledger.deposit(50_000)
    ledger.deposit(10_000)
    ledger.withdraw(2_500)
    assert report.money_moves(conn) == (60_000, 2_500)
