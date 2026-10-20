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
