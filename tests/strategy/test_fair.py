from datetime import timedelta

import numpy as np
import pytest

from gridiron_edge.strategy.devig import devig
from gridiron_edge.strategy.distributions import (
    NFL_ML_SIGMA,
    EmpiricalMarginModel,
    NormalModel,
    implied_mean_from_line,
    p_push,
    prop_p_over,
)
from gridiron_edge.strategy.fair import fair_spread_mean, fair_total_mean, fair_two_way
from gridiron_edge.types import Quote


def test_normal_symmetry():
    assert NormalModel(13).p_greater(0, 0) == pytest.approx(0.5)


def test_ml_sigma():
    assert NormalModel(NFL_ML_SIGMA).p_greater(3, 0) == pytest.approx(0.6060, abs=1e-3)


def test_empirical_keeps_key_number_mass():
    res = np.array([3] * 146 + [-3] * 146 + [7] * 88 + [-7] * 88 + list(range(-20, 21)) * 10)
    m = EmpiricalMarginModel(res, 13)
    assert m.p_greater(0, 2.5) - m.p_greater(0, 3.5) > 0.10
    assert p_push(m, 0, 3) > 0.10 and p_push(NormalModel(13), 0, 3) == 0


def test_empirical_falls_back_to_normal_when_small():
    m = EmpiricalMarginModel(np.array([3, -3, 7]), 13)
    assert m.p_greater(0, 0) == pytest.approx(0.5)
    assert p_push(m, 0, 3) == 0


def test_prop_roundtrip():
    mu = implied_mean_from_line(62.5, 0.55, "yards")
    assert prop_p_over(62.5, mu, "yards") == pytest.approx(0.55, abs=1e-6)
    assert prop_p_over(70.5, mu, "yards") < 0.55


def test_prop_count_poisson_and_roundtrip():
    # P(X >= 3) for Poisson(2.5)
    assert prop_p_over(2.5, 2.5, "count") == pytest.approx(0.4562, abs=1e-3)
    mu = implied_mean_from_line(4.5, 0.4, "count")
    assert prop_p_over(4.5, mu, "count") == pytest.approx(0.4, abs=1e-6)


def q(book, mk, side, d, t, point=None):
    return Quote("odds_api", book, "g", mk, side, point, d, t)


def test_fair_prefers_pinnacle(now):
    qs = [q("pinnacle", "h2h", "home", 1.8, now), q("pinnacle", "h2h", "away", 2.1, now),
          q("draftkings", "h2h", "home", 1.6, now), q("draftkings", "h2h", "away", 2.4, now)]
    p, src = fair_two_way(qs, "h2h", None, now=now)
    assert src == "pinnacle" and p == pytest.approx(devig([1.8, 2.1], "power")[0])


def test_fair_ignores_stale_and_falls_back(now):
    old = now - timedelta(hours=2)
    qs = [q("pinnacle", "h2h", "home", 1.8, old), q("pinnacle", "h2h", "away", 2.1, old),
          q("novig", "h2h", "home", 1.9, now), q("novig", "h2h", "away", 1.95, now)]
    assert fair_two_way(qs, "h2h", None, now=now)[1] == "exchanges"


def test_fair_median_fallback(now):
    qs = []
    for book, h, a in [("dk", 1.8, 2.1), ("fd", 1.85, 2.05), ("mgm", 1.7, 2.3)]:
        qs += [q(book, "h2h", "home", h, now), q(book, "h2h", "away", a, now)]
    p, src = fair_two_way(qs, "h2h", None, now=now)
    assert src == "median"
    assert p == pytest.approx(devig([1.8, 2.1], "power")[0])


def test_fair_none_when_one_sided(now):
    assert fair_two_way([q("pinnacle", "h2h", "home", 1.8, now)], "h2h", None, now=now) is None


def test_fair_requires_same_point(now):
    qs = [q("pinnacle", "spreads", "home", 1.95, now, -3.0), q("pinnacle", "spreads", "away", 1.95, now, 3.5)]
    assert fair_two_way(qs, "spreads", -3.0, now=now) is None


def test_fair_totals_additive(now):
    qs = [q("pinnacle", "totals", "over", 1.9, now, 45.5), q("pinnacle", "totals", "under", 1.95, now, 45.5)]
    p, src = fair_two_way(qs, "totals", 45.5, now=now)
    assert src == "pinnacle" and p == pytest.approx(devig([1.9, 1.95], "additive")[0])


def test_fair_spread_mean_even_price(now):
    qs = [q("pinnacle", "spreads", "home", 1.95, now, -3.0), q("pinnacle", "spreads", "away", 1.95, now, 3.0)]
    assert fair_spread_mean(qs, now=now) == pytest.approx(3.0, abs=1e-6)


def test_fair_spread_mean_shades_toward_favored_cover(now):
    qs = [q("pinnacle", "spreads", "home", 1.80, now, -3.0), q("pinnacle", "spreads", "away", 2.10, now, 3.0)]
    assert fair_spread_mean(qs, now=now) > 3.0


def test_fair_total_mean_and_none(now):
    qs = [q("pinnacle", "totals", "over", 1.95, now, 45.5), q("pinnacle", "totals", "under", 1.95, now, 45.5)]
    assert fair_total_mean(qs, now=now) == pytest.approx(45.5, abs=1e-6)
    assert fair_total_mean([], now=now) is None
