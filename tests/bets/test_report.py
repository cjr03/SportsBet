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
