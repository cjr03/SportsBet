import statistics
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
from gridiron_edge.strategy.fair import (
    fair_price,
    fair_spread_mean,
    fair_total_mean,
    fair_two_way,
    implied_market_mean,
)
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


# -- spread-conditional margin model (audit fair-pricing #2) ------------------------------------

def test_round_half_away_not_bankers():
    from gridiron_edge.strategy.distributions import _round_half_away
    assert list(_round_half_away(np.array([0.5, 1.5, 2.5, -0.5, -2.5]))) == [1, 2, 3, -1, -3]


def test_conditional_model_uses_games_near_the_spread():
    from gridiron_edge.strategy.distributions import ConditionalMarginModel
    # 3-point favourites win by exactly 3 a lot; 10-point favourites never do
    spreads = [3.0] * 600 + [10.0] * 600
    margins = [3] * 150 + list(range(-15, 30)) * 10 + [0] * 0
    margins = margins[:600] + [m + 7 for m in range(-20, 40)] * 10
    m = ConditionalMarginModel(spreads, margins, 13, min_ess=50)
    assert m.p_equal(3.0, 3) > 0.2
    assert m.p_equal(10.0, 3) < 0.05
    # the away side of a 3-point spread sees the mirrored mass at -3
    assert m.p_equal(-3.0, -3) == pytest.approx(m.p_equal(3.0, 3))
    # a half point moves the probability by the push mass at 3
    assert m.p_greater(3.0, 2.5) - m.p_greater(3.0, 3.5) == pytest.approx(m.p_equal(3.0, 3))
    assert p_push(m, 3.0, 3) == pytest.approx(m.p_equal(3.0, 3))
    assert m.p_equal(3.0, 3.5) == 0


def test_conditional_model_does_not_round_the_mean():
    from gridiron_edge.strategy.distributions import ConditionalMarginModel
    rng = np.random.default_rng(1)
    s = rng.choice(np.arange(-14, 14.5, 0.5), 5000)
    marg = np.rint(s + rng.normal(0, 13, 5000))
    m = ConditionalMarginModel(s, marg, 13)
    # the old model returned the same value for every mean in [2.75, 3.75)
    assert m.p_greater(2.8, 3.5) < m.p_greater(3.6, 3.5)
    assert m.p_greater(0.0, 0.0) + m.p_equal(0.0, 0) + m.p_greater(0.0, 0.0) == pytest.approx(1.0)


def test_conditional_model_falls_back_to_normal_when_small():
    from gridiron_edge.strategy.distributions import ConditionalMarginModel
    m = ConditionalMarginModel([3, -3, 7], [3, -3, 7], 13)
    assert m.p_greater(0, 0) == pytest.approx(0.5) and p_push(m, 0, 3) == 0


def test_empirical_rounds_half_away():
    m = EmpiricalMarginModel(np.array([2.5] * 600), 13)
    assert p_push(m, 0, 3) == pytest.approx(1.0)


def test_implied_mean_is_push_aware():
    from gridiron_edge.strategy.distributions import ConditionalMarginModel, implied_mean, p_cover
    rng = np.random.default_rng(2)
    s = rng.choice(np.arange(-14, 14.5, 0.5), 6000)
    marg = np.rint(s + rng.normal(0, 13, 6000))
    marg[(np.abs(s - 3) <= 1) & (rng.random(6000) < 0.12)] = 3  # push mass at 3 for 3-point favourites
    m = ConditionalMarginModel(s, marg, 13)
    mu = implied_mean(m, 3, 0.45)
    assert mu is not None and p_cover(m, mu, 3) == pytest.approx(0.45, abs=5e-3)
    # P(cover | no push) is not P(M > 3): ignoring the push gives a different mean
    assert m.p_greater(mu, 3) < 0.45
    # the normal model has no push, so the inversion is the exact normal quantile
    from scipy.stats import norm
    mu_n = implied_mean(NormalModel(13), 3.5, 0.6)
    assert mu_n == pytest.approx(3.5 + 13 * norm.ppf(0.6), abs=1e-4)


# -- reference set, staleness, anchoring, tiers (audit fair-pricing #1, #3, #4, #5) -------------

def pair(book, mk, a, b, t, point=None, source="odds_api", fetched=None):
    sa, sb = {"h2h": ("home", "away"), "spreads": ("home", "away"), "totals": ("over", "under")}[mk]
    pb = -point if (mk == "spreads" and point is not None) else point
    return [Quote(source, book, "g", mk, sa, point, a, t, fetched),
            Quote(source, book, "g", mk, sb, pb, b, t, fetched)]


def test_kalshi_and_polymarket_never_in_reference(now):
    qs = (pair("draftkings", "h2h", 1.8, 2.1, now) + pair("kalshi", "h2h", 1.5, 2.9, now)
          + pair("polymarket", "h2h", 1.5, 2.9, now))
    fp = fair_price(qs, "h2h", None, now=now)
    assert fp.source == "median" and fp.books == ("draftkings",)
    assert fair_two_way(pair("kalshi", "h2h", 1.8, 2.1, now), "h2h", None, now=now) is None


def test_staleness_uses_fetch_time(now):
    # the book's price hasn't changed in hours, but we pulled it a minute ago: still usable
    qs = pair("pinnacle", "h2h", 1.8, 2.1, now - timedelta(hours=3), fetched=now - timedelta(minutes=1))
    assert fair_two_way(qs, "h2h", None, now=now)[1] == "pinnacle"
    stale = pair("pinnacle", "h2h", 1.8, 2.1, now, fetched=now - timedelta(minutes=30))
    assert fair_two_way(stale, "h2h", None, now=now, anchor_max_seconds=0) is None


def test_stale_seconds_is_threaded_through(now):
    qs = pair("pinnacle", "spreads", 1.95, 1.95, now - timedelta(minutes=20), -3.0)
    assert fair_spread_mean(qs, now=now, stale_seconds=600) is None
    assert fair_spread_mean(qs, now=now, stale_seconds=3600) == pytest.approx(3.0, abs=1e-4)


def _anchor_setup(now, dk_now, fd_now, pulled_ago=timedelta(hours=5), point=None, mk="h2h"):
    pulled = now - pulled_ago
    qs = pair("pinnacle", mk, 1.80, 2.10, pulled, point)
    qs += pair("draftkings", mk, 1.77, 2.08, pulled, point)
    qs += pair("fanduel", mk, 1.78, 2.06, pulled, point)
    qs += pair("draftkings", mk, *dk_now, now, point, source="therundown", fetched=now)
    qs += pair("fanduel", mk, *fd_now, now, point, source="therundown", fetched=now)
    return qs


def test_anchored_fair_shifts_last_sharp_by_consensus_move(now):
    qs = _anchor_setup(now, (1.74, 2.12), (1.75, 2.10))
    fp = fair_price(qs, "h2h", None, now=now)
    assert fp.source == "anchored-pinnacle" and "pinnacle" in fp.books and "draftkings" in fp.books
    pin = devig([1.80, 2.10], "power")[0]
    med = lambda pairs: statistics.median(devig(list(x), "power")[0] for x in pairs)  # noqa: E731
    move = med([(1.74, 2.12), (1.75, 2.10)]) - med([(1.77, 2.08), (1.78, 2.06)])
    assert 0 < move <= 0.015
    assert fp.p == pytest.approx(pin + move)


def test_anchor_void_when_consensus_moves_too_far(now):
    qs = _anchor_setup(now, (1.55, 2.55), (1.56, 2.50))
    fp = fair_price(qs, "h2h", None, now=now)
    assert fp.source == "median"


def test_anchor_void_when_spread_crosses_key_number(now):
    pulled = now - timedelta(hours=5)
    qs = pair("pinnacle", "spreads", 1.95, 1.95, pulled, -2.5)
    qs += pair("draftkings", "spreads", 1.91, 1.91, pulled, -2.5)
    # now the soft books hang -3.5 as the main line, and -2.5 is juiced but close in probability
    qs += pair("draftkings", "spreads", 1.92, 1.90, now, -2.5, source="therundown", fetched=now)
    qs += pair("draftkings", "spreads", 1.91, 1.91, now, -3.5, source="therundown", fetched=now)
    assert fair_price(qs, "spreads", -2.5, now=now).source == "median"


def test_no_anchor_without_fresh_consensus(now):
    qs = pair("pinnacle", "h2h", 1.8, 2.1, now - timedelta(hours=5))
    assert fair_price(qs, "h2h", None, now=now) is None


def test_exchange_hold_cap_and_weights(now):
    wide = pair("novig", "h2h", 1.70, 2.00, now)  # ~8.8% hold: dropped
    fp = fair_price(wide + pair("draftkings", "h2h", 1.8, 2.1, now), "h2h", None, now=now)
    assert fp.source == "median"
    tight = pair("novig", "h2h", 1.95, 2.02, now) + pair("prophetx", "h2h", 1.90, 2.00, now)
    fp = fair_price(tight, "h2h", None, now=now)
    assert fp.source == "exchanges" and fp.books == ("novig", "prophetx")
    lo, hi = sorted([devig([1.95, 2.02], "power")[0], devig([1.90, 2.00], "power")[0]])
    assert lo < fp.p < hi


def test_mid_tier_books_beat_median(now):
    qs = pair("lowvig", "h2h", 1.85, 2.08, now) + pair("draftkings", "h2h", 1.7, 2.3, now)
    assert fair_price(qs, "h2h", None, now=now).source == "mid"


def test_implied_mean_prefers_sharper_tier_over_nearer_even(now):
    # median books have -3.5 at exactly even; pinnacle only quotes -3 at a lean
    qs = pair("draftkings", "spreads", 1.91, 1.91, now, -3.5)
    qs += pair("pinnacle", "spreads", 1.85, 2.02, now, -3.0)
    got = implied_market_mean(qs, "spreads", NormalModel(13), now=now)
    assert got.point == -3.0 and got.fair.source == "pinnacle"
