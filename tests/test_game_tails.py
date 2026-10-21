"""Kalshi game contracts far from the sportsbook line: translation limit, shrink to the Kalshi mid,
and a gap check (the replay's 40 tail 'edges' priced off a Normal curve shifted 10-20 points)."""
from dataclasses import replace

import numpy as np
import pytest
from scipy.stats import norm

from gridiron_edge.pipeline import kalshi_game_candidates, middle_candidates
from gridiron_edge.strategy.distributions import CFB_SIGMA, NFL_SPREAD_SIGMA, NFL_TOTAL_SIGMA, EmpiricalMarginModel
from gridiron_edge.types import Quote

from tests.test_pipeline import NOW, game, snap_with, spreads


def totals(gid, point, dec=1.95, book="pinnacle"):
    return [Quote("odds_api", book, gid, "totals", "over", point, dec, NOW),
            Quote("odds_api", book, gid, "totals", "under", point, dec, NOW)]


def _cfb(**kw):
    return snap_with(games={"g1": game(1, league="cfb")}, **kw)


def test_cfb_ladder_strike_far_from_book_line_is_no_rec(mk, settings):
    # replay: book Rice (away) +7.5; Kalshi "Rice win by 8+" at 11/13c. Normal(15.75) said ~17%.
    m = mk("E1-A18", "E1", "spread", "A1", line=7.5, yes_bid=0.11, yes_ask=0.13, no_ask=0.89, game_id="g1")
    recs = kalshi_game_candidates(_cfb(quotes=spreads("g1", -7.5), kalshi=[m]), settings)
    assert all(r.ev < 0.05 for r in recs)
    assert recs == []  # 15 points from the book line: beyond the CFB limit of 10


def test_nfl_total_far_from_book_total_is_dropped(mk, settings):
    # replay: book total 44; Kalshi "under 32.5" bought at 15c (YES = over 32.5). Normal(13): ~19%.
    m = mk("E1-T32", "E1", "total", None, line=32.5, yes_bid=0.85, yes_ask=0.86, no_ask=0.15, game_id="g1")
    assert norm.cdf(32.5, 44.0, NFL_TOTAL_SIGMA) > 0.18
    assert kalshi_game_candidates(snap_with(quotes=totals("g1", 44.0), kalshi=[m]), settings) == []


def test_translated_strike_is_shrunk_toward_the_kalshi_mid(mk, settings):
    settings = replace(settings, kalshi_min_edge=0.0)  # pricing under test, not the edge filter
    # CFB book H1 -7.5 (home margin 7.5); Kalshi "H1 win by 12+" (home margin > 11.5): 4 points away
    m = mk("E1-H112", "E1", "spread", "H1", line=11.5, yes_bid=0.33, yes_ask=0.35, no_ask=0.67, game_id="g1")
    ours = norm.sf(11.5, 7.5, CFB_SIGMA)
    (r,) = kalshi_game_candidates(_cfb(quotes=spreads("g1", -7.5), kalshi=[m]), settings)
    w = r.why["weight"]
    assert 0 < w < 1 and r.why["kalshi_mid"] == pytest.approx(0.34) and r.why["book_shift"] == 4.0
    assert r.why["p_ours"] == pytest.approx(ours, abs=1e-3)
    assert r.legs[0].side == "yes"
    assert r.legs[0].p_fair == pytest.approx(w * ours + (1 - w) * 0.34, abs=1e-3)
    # further from the line, less of our number is kept
    m2 = replace(m, ticker="E1-H115", line=14.5, yes_bid=0.25, yes_ask=0.27, no_ask=0.75)
    (r2,) = kalshi_game_candidates(_cfb(quotes=spreads("g1", -7.5), kalshi=[m2]), settings)
    assert r2.why["book_shift"] == 7.0 and r2.why["weight"] < w


def test_exact_line_keeps_the_sharp_number(mk, settings):
    m = mk("E1-H13", "E1", "spread", "H1", line=2.5, yes_bid=0.44, yes_ask=0.45, no_ask=0.56, game_id="g1")
    quotes = spreads("g1", -2.5)
    quotes[0], quotes[1] = replace(quotes[0], decimal=1.80), replace(quotes[1], decimal=2.05)
    (r,) = kalshi_game_candidates(snap_with(quotes=quotes, kalshi=[m]), settings)
    assert r.why["book_shift"] == 0.0 and r.why.get("weight", 1.0) == 1.0


def test_our_number_far_from_a_liquid_mid_is_dropped(mk, settings):
    # NFL book home -3 (mean 3); Kalshi "H1 win by 8+": 4.5 points away, ours ~33%, mid 20%: 13 points off
    m = mk("E1-H18", "E1", "spread", "H1", line=7.5, yes_bid=0.19, yes_ask=0.21, no_ask=0.81, game_id="g1")
    ours = norm.sf(7.5, 3.0, NFL_SPREAD_SIGMA)
    assert ours - 0.20 > 0.08
    assert kalshi_game_candidates(snap_with(quotes=spreads("g1", -3.0), kalshi=[m]), settings) == []


def test_translated_strike_needs_a_liquid_mid(mk, settings):
    # 4.5 points from the line on a 12c-wide Kalshi book: nothing to check the curve against
    m = mk("E1-H18", "E1", "spread", "H1", line=7.5, yes_bid=0.15, yes_ask=0.27, no_ask=0.85, game_id="g1")
    assert kalshi_game_candidates(snap_with(quotes=spreads("g1", -3.0), kalshi=[m]), settings) == []


def test_middle_legs_far_from_the_book_line_are_skipped(mk, settings):
    resid = np.concatenate([np.full(300, 0), np.full(80, 4), np.full(80, -4), np.arange(-20, 21).repeat(4)])
    model = EmpiricalMarginModel(resid, NFL_SPREAD_SIGMA)
    ladder = [mk("E2-H13", "E2", "spread", "H1", line=2.5, yes_ask=0.52, no_ask=0.49, game_id="g1"),
              mk("E2-H14", "E2", "spread", "H1", line=3.5, yes_ask=0.51, no_ask=0.50, game_id="g1")]
    near = replace(snap_with(kalshi=ladder, quotes=spreads("g1", -3.0)),
                   margin_models={**snap_with().margin_models, "nfl_spread": model})
    assert middle_candidates(near, settings)  # legs at the book line: a middle
    # the same ladder 9 points from the book line, on a model that puts mass there: no middle
    far_model = EmpiricalMarginModel(resid - 9, NFL_SPREAD_SIGMA)
    far = replace(near, quotes=spreads("g1", -12.0),
                  margin_models={**near.margin_models, "nfl_spread": far_model})
    assert middle_candidates(far, settings) == []
