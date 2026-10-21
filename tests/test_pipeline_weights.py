"""Model weight in the game blend: fitted weight, disagreement cap, QB veto, NFL totals, CFB."""
from dataclasses import replace

import pytest

from gridiron_edge.models import Prediction
from gridiron_edge.models.blend import default_weight
from gridiron_edge.pipeline import _game_p_yes, game_p_yes_w, kalshi_game_candidates, market_p_yes, model_weight
from gridiron_edge.types import Quote

from test_pipeline import NOW, game, h2h, snap_with, spreads


def totals(gid, point, dec=1.95):
    return [Quote("odds_api", "pinnacle", gid, "totals", "over", point, dec, NOW),
            Quote("odds_api", "pinnacle", gid, "totals", "under", point, dec, NOW)]


def _spread_snap(mk, mean_margin, league="nfl", **kw):
    m = mk("E1-H13", "E1", "spread", "H1", line=2.5, yes_ask=0.40, no_ask=0.61, game_id="g1")
    pred = Prediction("g1", mean_margin, None, "t")
    return m, snap_with(games={"g1": game(1, league=league)}, quotes=spreads("g1", -3.0), kalshi=[m],
                        predictions={"g1": pred}, **kw)


def test_model_weight_uses_fit_minus_se_capped_at_default():
    snap = snap_with(blend_fits={("nfl", "spread"): (0.30, 0.20), ("cfb", "spread"): (1.5, 0.3)})
    assert model_weight("nfl", "spread", 48, snap) == pytest.approx(0.10)
    assert model_weight("cfb", "spread", 48, snap) == default_weight("cfb", 48)
    assert model_weight("nfl", "ml", 48, snap) == default_weight("nfl", 48)  # no fit: prior default
    snap = snap_with(blend_fits={("nfl", "spread"): (0.1, 0.3)})
    assert model_weight("nfl", "spread", 48, snap) == 0.0


def test_nfl_totals_are_market_only_until_validated(mk, settings):
    m = mk("E1-T45", "E1", "total", None, line=44.5, yes_ask=0.40, game_id="g1")
    pred = Prediction("g1", 0.0, 48.0, "t")
    snap = snap_with(quotes=totals("g1", 44.5), kalshi=[m], predictions={"g1": pred})
    p, w = game_p_yes_w(m, game(1), snap, settings)
    assert w == 0.0 and p == pytest.approx(market_p_yes(m, game(1), snap, settings))
    snap = replace(snap, blend_fits={("nfl", "total"): (0.9, 0.2)})
    p2, w2 = game_p_yes_w(m, game(1), snap, settings)
    assert w2 > 0 and p2 > p


def test_disagreement_cap_drops_model(mk, settings):
    m, snap = _spread_snap(mk, 3.0 + 4.0)  # 4 pts off the market: under the 5-pt default cap
    _, w = game_p_yes_w(m, game(1), snap, settings)
    assert w > 0
    m, snap = _spread_snap(mk, 3.0 + 7.0)  # 7 pts off: more likely missing information
    p, w = game_p_yes_w(m, game(1), snap, settings)
    assert w == 0.0 and p == pytest.approx(market_p_yes(m, game(1), snap, settings))
    m, snap = _spread_snap(mk, 3.0 + 4.0, disagree_caps={"nfl": 3.5})  # fitted cap wins
    assert game_p_yes_w(m, game(1), snap, settings)[1] == 0.0


def test_qb_veto_drops_model(mk, settings):
    m, snap = _spread_snap(mk, 5.0)
    snap.predictions["g1"] = replace(snap.predictions["g1"], veto="QB change: KC")
    assert game_p_yes_w(m, game(1), snap, settings)[1] == 0.0


def test_cfb_pure_market_edge_without_disagreement(mk, settings):
    g = game(1, league="cfb")
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=0.40, game_id="g1")
    quotes = h2h("g1", 1.80, 2.10) + spreads("g1", -3.0)
    close = Prediction("g1", 4.0, None, "t")
    snap = snap_with(games={"g1": g}, quotes=quotes, kalshi=[m], predictions={"g1": close})
    p, w = game_p_yes_w(m, g, snap, settings)
    assert w == 0.0 and p == pytest.approx(market_p_yes(m, g, snap, settings))
    assert len(kalshi_game_candidates(snap, settings)) == 1  # the Kalshi-vs-book edge alone
    no_model = replace(snap, predictions={})
    assert _game_p_yes(m, g, no_model, settings) == pytest.approx(p)


def test_cfb_gated_ignores_model_even_when_it_disagrees(mk, settings):
    g = game(1, league="cfb")
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=0.40, game_id="g1")
    quotes = h2h("g1", 1.80, 2.10) + spreads("g1", -3.0)
    far = Prediction("g1", 8.0, None, "t")
    snap = snap_with(games={"g1": g}, quotes=quotes, kalshi=[m], predictions={"g1": far})
    assert game_p_yes_w(m, g, snap, settings)[1] > 0
    gated = replace(snap, gates={("cfb", "ml"): True})
    assert game_p_yes_w(m, g, gated, settings)[1] == 0.0


def test_market_only_rec_live_when_setting_on(mk, settings):
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=0.48, game_id="g1")
    snap = snap_with(quotes=h2h("g1", 1.80, 2.10), kalshi=[m], gates={("nfl", "ml"): True})
    assert kalshi_game_candidates(snap, settings)[0].observe_only
    assert not kalshi_game_candidates(snap, replace(settings, market_only_live=True))[0].observe_only
    pred = Prediction("g1", 3.0, None, "t")  # the model contributes: the gate still applies
    with_model = replace(snap, predictions={"g1": pred}, quotes=snap.quotes + spreads("g1", -3.0))
    assert kalshi_game_candidates(with_model, replace(settings, market_only_live=True))[0].observe_only


def test_market_only_live_never_overrides_a_pause(mk, settings):
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=0.48, game_id="g1")
    key = ("nfl", "ml")
    snap = snap_with(quotes=h2h("g1", 1.80, 2.10), kalshi=[m], gates={key: True}, paused={key: True})
    # drift / CLV throttle paused the market: a market-only rec stays observe-only (no text)
    assert kalshi_game_candidates(snap, replace(settings, market_only_live=True))[0].observe_only
