import pytest

from gridiron_edge.strategy.arb import Arb, cross_venue, filter_arbs, kalshi_ladders, kalshi_ml_pairs


def test_cross_venue_example():
    roi, st = cross_venue({"a": (2.10, 0), "b": (0, 2.05)})
    assert roi == pytest.approx(0.0373, abs=1e-4) and st["a"] == pytest.approx(0.4940, abs=1e-3)


def test_cross_venue_none_when_no_arb():
    assert cross_venue({"a": (1.90, 0), "b": (0, 1.90)}) is None
    assert cross_venue({"a": (2.10, 0)}) is None


def test_ml_pair_found_with_fees(mk):
    arbs = kalshi_ml_pairs([mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.45, yes_ask_size=100),
                            mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.45, yes_ask_size=50)], lambda m: "nfl")
    assert len(arbs) == 1 and arbs[0].qty == 50 and arbs[0].tie_risk and 0 < arbs[0].roi < 0.1


def test_ml_pair_none_when_fees_eat_it(mk):
    assert kalshi_ml_pairs([mk("E1-A", "E1", "ml", "A", yes_ask=0.50),
                            mk("E1-B", "E1", "ml", "B", yes_ask=0.49)], lambda m: "nfl") == []


def test_ml_pair_requires_exactly_two_active(mk):
    ms = [mk("E1-A", "E1", "ml", "A", yes_ask=0.40), mk("E1-B", "E1", "ml", "B", yes_ask=0.40),
          mk("E1-C", "E1", "ml", "C", yes_ask=0.40)]
    assert kalshi_ml_pairs(ms, lambda m: "cfb") == []
    ms = [mk("E1-A", "E1", "ml", "A", yes_ask=0.40), mk("E1-B", "E1", "ml", "B", yes_ask=0.40, status="closed")]
    assert kalshi_ml_pairs(ms, lambda m: "cfb") == []


def test_ml_pair_cfb_no_tie_risk(mk):
    arbs = kalshi_ml_pairs([mk("E1-A", "E1", "ml", "A", yes_ask=0.40),
                            mk("E1-B", "E1", "ml", "B", yes_ask=0.40)], lambda m: "cfb")
    assert len(arbs) == 1 and not arbs[0].tie_risk


def test_ladder_violation(mk):
    lo = mk("E2-PHI7", "E2", "spread", "PHI", line=6.5, yes_bid=0.30, yes_ask=0.31)
    hi = mk("E2-PHI8", "E2", "spread", "PHI", line=7.5, yes_bid=0.36, yes_ask=0.37, no_ask=0.64)
    a = kalshi_ladders([lo, hi])
    assert len(a) == 1 and a[0].roi > 0 and a[0].kind == "kalshi_ladder"


def test_ladder_consistent_none(mk):
    lo = mk("E3-PHI7", "E3", "spread", "PHI", line=6.5, yes_bid=0.16, yes_ask=0.18)
    hi = mk("E3-PHI8", "E3", "spread", "PHI", line=7.5, yes_bid=0.13, yes_ask=0.14)
    assert kalshi_ladders([lo, hi]) == []


def test_filter_drops_absurd_roi():
    assert filter_arbs([Arb("kalshi_ml_pair", "g", [], 10, 5, 10, 1.0, False)], 0.08) == []
    assert filter_arbs([Arb("kalshi_ml_pair", "g", [], 0, 5, 10, 0.05, False)], 0.08) == []
    keep = Arb("kalshi_ml_pair", "g", [], 10, 5, 10, 0.05, False)
    assert filter_arbs([keep], 0.08) == [keep]
