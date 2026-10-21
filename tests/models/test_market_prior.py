"""Market-implied ratings (ridge on closing spreads) as an early-season prior."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from gridiron_edge.models.cfb import cfb_prior
from gridiron_edge.models.nfl import NflRatings
from gridiron_edge.models.ridge import implied_ratings

TEAMS = [f"T{i:02d}" for i in range(32)]


def test_implied_ratings_recover_spreads():
    rng = np.random.default_rng(2)
    true = dict(zip(TEAMS, rng.normal(0, 6, 32)))
    rows = []
    for _ in range(400):
        h, a = rng.choice(TEAMS, 2, replace=False)
        neutral = rng.random() < 0.1
        rows.append((h, a, neutral, true[h] - true[a] + (0 if neutral else 2.0), 1.0))
    ratings, hfa = implied_ratings(rows, lam=0.1)
    assert hfa == pytest.approx(2.0, abs=0.1)
    assert ratings["T00"] - ratings["T01"] == pytest.approx(true["T00"] - true["T01"], abs=0.2)
    assert implied_ratings([], 1.0) == ({}, 0.0)


def _sched(spread_for_t00: float, future_spread: float = 0.0):
    rows = []
    for wk in range(8):
        for k in range(16):
            h, a = TEAMS[(2 * k + wk) % 32], TEAMS[(2 * k + 1 + 3 * wk) % 32]
            if h == a:
                continue
            line = spread_for_t00 if h == "T00" else (-spread_for_t00 if a == "T00" else 0.0)
            rows.append({"season": 2025, "week": wk + 1, "gameday": date(2025, 9, 7) + timedelta(days=7 * wk),
                         "home_team": h, "away_team": a, "home_score": 21, "away_score": 21,
                         "spread_line": line + 2.0, "location": "Home"})
    rows.append({"season": 2025, "week": 9, "gameday": date(2025, 11, 9), "home_team": "T00",
                 "away_team": "T05", "home_score": None, "away_score": None,
                 "spread_line": future_spread, "location": "Home"})
    return pl.DataFrame(rows)


def test_nfl_market_component_moves_ratings():
    asof = date(2025, 11, 1)
    flat = NflRatings.fit(_sched(0.0), None, asof)
    fav = NflRatings.fit(_sched(7.0), None, asof)
    assert fav.ratings["T00"] > flat.ratings["T00"] + 0.5
    # future games' lines are not used
    leak = NflRatings.fit(_sched(7.0, future_spread=40.0), None, asof)
    assert leak.ratings["T00"] == pytest.approx(fav.ratings["T00"])


def test_nfl_without_lines_keeps_points_epa_blend():
    s = _sched(0.0).drop("spread_line")
    m = NflRatings.fit(s, None, date(2025, 11, 1))
    assert m.ratings["T00"] == pytest.approx(0.0, abs=1e-6)


def test_cfb_prior_blends_market_rating():
    prev = {"A": -10.0, "B": -30.0}
    base = cfb_prior(prev, {}, {})
    mkt = cfb_prior(prev, {}, {}, market={"A": 20.0, "B": -20.0})
    assert base["A"] == pytest.approx(6.0)
    assert mkt["A"] == pytest.approx(0.6 * (0.5 * 10 + 0.5 * 20))
