import pytest

from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev, passes_filters, taker_fee
from gridiron_edge.strategy.kelly import Bet, kelly_binary, kelly_push, optimize_slate, to_cents


def test_taker_fee_examples():
    assert taker_fee(100, 0.50) == pytest.approx(1.75) and taker_fee(1, 0.50) == pytest.approx(0.02)
    assert taker_fee(10, 0.95) == pytest.approx(0.04)


def test_effective_price_and_ev():
    pe = effective_price(0.46, 40)
    assert pe == pytest.approx(0.46 + taker_fee(40, .46) / 40)
    assert kalshi_ev(0.512, pe) == pytest.approx(0.512 / pe - 1)


@pytest.mark.parametrize("price,reason", [(0.05, "price_band"), (0.95, "price_band")])
def test_price_band(settings, price, reason):
    assert passes_filters(0.99, price, effective_price(price, 10), 1000, 10, settings, "active") == (False, reason)


def test_filters_reasons(settings):
    assert passes_filters(0.60, 0.50, 0.52, 5, 10, settings, "active") == (False, "depth")
    assert passes_filters(0.60, 0.50, 0.52, 1000, 10, settings, "closed") == (False, "status")
    assert passes_filters(0.53, 0.50, 0.52, 1000, 10, settings, "active") == (False, "ev")
    assert passes_filters(0.60, 0.50, 0.52, 1000, 10, settings, "active") == (True, "ok")


def test_filter_edge_reason(settings):
    # EV 3.2% passes, but edge 0.016 -> use p_eff high so EV ok and edge < 1.5pp
    assert passes_filters(0.2, 0.15, 0.1945, 1000, 10, settings, "active") == (False, "edge")


def test_kelly_binary():
    assert kelly_binary(0.55, 2.0) == pytest.approx(0.10) and kelly_binary(0.4, 2.0) == 0


def test_kelly_push():
    assert kelly_push(0.55, 0.45, 2.0) == pytest.approx(0.10)
    assert kelly_push(0.4, 0.4, 2.0) == 0


def test_slate_single_matches_fractional_kelly():
    assert optimize_slate([Bet(0.55, 0, 2.0, "g1")], 0.25, 1, 1, 1)[0] == pytest.approx(0.025, abs=1e-3)


def test_slate_respects_caps():
    f = optimize_slate([Bet(0.65, 0, 2.0, f"g{i}") for i in range(12)], 0.25, 0.02, 0.04, 0.20)
    assert max(f) <= 0.02 + 1e-9 and sum(f) <= 0.20 + 1e-9


def test_slate_per_game_cap():
    assert sum(optimize_slate([Bet(0.65, 0, 2.0, "g1")] * 3, 0.25, 0.02, 0.04, 0.2)) <= 0.04 + 1e-9


def test_slate_empty_and_negative_ev():
    assert optimize_slate([], 0.25, 0.02, 0.04, 0.2) == []
    assert optimize_slate([Bet(0.4, 0, 2.0, "g1")], 0.25, 1, 1, 1) == [pytest.approx(0, abs=1e-9)]


def test_slate_fallback_when_solver_fails(monkeypatch):
    import gridiron_edge.strategy.kelly as k

    def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(k, "minimize", boom)
    f = optimize_slate([Bet(0.65, 0, 2.0, "g1")] * 3, 0.25, 0.02, 0.04, 0.2)
    assert sum(f) <= 0.04 + 1e-9 and max(f) <= 0.02 + 1e-9 and min(f) > 0


def test_to_cents_min_stake():
    assert to_cents([0.0005, 0.02], 100_000, 100) == [0, 2_000]


def test_fallback_sizes_by_edge_and_zeroes_negative_ev(monkeypatch):
    import gridiron_edge.strategy.kelly as k

    def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(k, "minimize", boom)
    bets = [Bet(0.4, 0, 2.0, "g1"), Bet(0.55, 0, 2.0, "g2"), Bet(0.7, 0, 2.0, "g3")]
    f = optimize_slate(bets, 0.25, 1, 1, 1)
    assert f[0] == 0
    assert 0 < f[1] < f[2]
    assert f[1] == pytest.approx(k.kelly_push(0.55, 0.45, 2.0) * 0.25)


# -- quant upgrade: scenarios, per-bet scales, headroom caps, correlation, partial losses --------

def test_scenarios_two_outcomes_exact_up_to_15_bets():
    from gridiron_edge.strategy.kelly import _scenarios
    bets = [Bet(0.55, 0, 2.0, f"g{i}") for i in range(15)]
    R, w = _scenarios(bets, 0)
    assert R.shape == (2 ** 15, 15) and w.sum() == pytest.approx(1.0)
    assert set(map(float, R[:, 0])) == {1.0, -1.0}  # win/loss only: no push column
    pushy = [Bet(0.5, 0.1, 2.0, f"g{i}") for i in range(9)]
    R3, _ = _scenarios(pushy, 0)
    assert R3.shape == (3 ** 9, 9) and 0.0 in set(map(float, R3[:, 0]))


def test_scenarios_monte_carlo_uses_100k_draws():
    from gridiron_edge.strategy.kelly import _MC_DRAWS, _scenarios
    R, w = _scenarios([Bet(0.55, 0, 2.0, f"g{i}") for i in range(20)], 0)
    assert _MC_DRAWS == 100_000 and R.shape == (100_000, 20)
    assert (R[:, 3] > 0).mean() == pytest.approx(0.55, abs=0.01)


def test_scale_shrinks_stake():
    # 55% at even money: 1/4 Kelly is 2.5%; a 0.5 source multiplier halves it to 1.25%
    f = optimize_slate([Bet(0.55, 0, 2.0, "g1")], 0.25, 1, 1, 1, scales=[0.5])
    assert f[0] == pytest.approx(0.0125, abs=1e-3)
    assert optimize_slate([Bet(0.55, 0, 2.0, "g1")], 0.25, 1, 1, 1, scales=[0.0]) == [0.0]


def test_per_game_caps_and_groups_bind():
    bets = [Bet(0.65, 0, 2.0, "g1"), Bet(0.65, 0, 2.0, "g2"), Bet(0.65, 0, 2.0, "g3")]
    f = optimize_slate(bets, 0.25, 0.02, 0.04, 0.2, game_caps={"g1": 0.005, "g2": 0.0})
    assert f[0] <= 0.005 + 1e-9 and f[1] == pytest.approx(0, abs=1e-9) and f[2] == pytest.approx(0.02, abs=1e-6)
    g = optimize_slate(bets, 0.25, 0.02, 0.04, 0.2, groups=[([0, 1, 2], 0.03)])
    assert sum(g) <= 0.03 + 1e-9


def test_fallback_honours_game_caps_groups_and_scales(monkeypatch):
    import gridiron_edge.strategy.kelly as k
    monkeypatch.setattr(k, "minimize", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    bets = [Bet(0.65, 0, 2.0, "g1"), Bet(0.65, 0, 2.0, "g2"), Bet(0.55, 0, 2.0, "g3")]
    f = optimize_slate(bets, 0.25, 0.02, 0.04, 0.2, scales=[1, 1, 0.5], game_caps={"g1": 0.01},
                       groups=[([1, 2], 0.015)])
    assert f[0] <= 0.01 + 1e-9 and f[1] + f[2] <= 0.015 + 1e-9
    assert f[2] > 0


def test_positive_correlation_shrinks_joint_stake():
    bets = [Bet(0.56, 0, 2.0, "g1"), Bet(0.56, 0, 2.0, "g1")]
    indep = optimize_slate(bets, 1.0, 1, 1, 1)
    corr = optimize_slate(bets, 1.0, 1, 1, 1, corr=[[1.0, 0.6], [0.6, 1.0]])
    assert sum(corr) < sum(indep) - 0.01


def test_partial_loss_bet():
    # a middle: costs 1.02, pays 2 with 10% (2/1.02 - 1 return), else 1 back (1/1.02 - 1 return)
    c = 1.02
    f = optimize_slate([Bet(0.10, 0, 2 / c, "g1", loss=1 / c - 1)], 1.0, 1, 1, 1)
    assert f[0] > 0.2  # a near-riskless edge earns a big full-Kelly fraction
    assert optimize_slate([Bet(0.01, 0, 2 / c, "g1", loss=1 / c - 1)], 1.0, 1, 1, 1)[0] == pytest.approx(0, abs=1e-6)


def test_clv_throttle():
    from gridiron_edge.strategy.kelly import clv_throttle
    assert clv_throttle([-0.05] * 5) == 1.0  # too few bets to judge
    assert clv_throttle([0.02, 0.01, 0.03, -0.01] * 5) == 1.0
    assert clv_throttle([0.02, -0.03, 0.01, -0.02] * 5) == 0.5  # mean below 0, not significant
    assert clv_throttle([-0.02, -0.03, -0.01, 0.005] * 5) == 0.0  # t < -2: observe only


def test_shrunk_bet_never_gets_more_than_identical_unshrunk_when_cap_binds():
    # review repro: a binding game cap used to favour the 0.5-shrunk bet 2.5x
    bets = [Bet(0.6, 0, 2.0, "g1"), Bet(0.6, 0, 2.0, "g1")]
    f = optimize_slate(bets, 0.25, 0.02, 0.02, 0.2, scales=[1, 0.5])
    assert f[1] <= f[0] + 1e-6
    assert sum(f) <= 0.02 + 1e-9
    assert f[1] <= 0.5 * 0.2 * 0.25 + 1e-9  # never above its shrunk single-bet Kelly
