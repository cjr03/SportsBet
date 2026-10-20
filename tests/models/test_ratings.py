from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from gridiron_edge.models.cfb import CfbRatings, cfb_prior
from gridiron_edge.models.nfl import NflRatings

TEAMS = [f"T{i:02d}" for i in range(32)]


def _nfl_schedule(seed=0):
    rng = np.random.default_rng(seed)
    true = rng.normal(0, 6, 32)
    rows = []
    for season in (2023, 2024, 2025):
        start = date(season, 9, 7)
        for wk in range(17):
            order = rng.permutation(32)
            for k in range(16):
                h, a = order[2 * k], order[2 * k + 1]
                margin = true[h] - true[a] + 2.0 + rng.normal(0, 12)
                total = 45 + rng.normal(0, 8)
                rows.append({"season": season, "week": wk + 1,
                             "gameday": start + timedelta(days=7 * wk),
                             "home_team": TEAMS[h], "away_team": TEAMS[a],
                             "home_score": int(round((total + margin) / 2)),
                             "away_score": int(round((total - margin) / 2)),
                             "neutral": False})
    return pl.DataFrame(rows), true


def test_nfl_recovers_ratings():
    sched, true = _nfl_schedule()
    m = NflRatings.fit(sched, None, date(2025, 12, 31))
    fitted = np.array([m.ratings[t] for t in TEAMS])
    assert np.corrcoef(fitted, true)[0, 1] > 0.8
    assert 1.0 < m.hfa < 3.0
    assert m.predict(TEAMS[0], TEAMS[1]) == pytest.approx(
        m.ratings[TEAMS[0]] - m.ratings[TEAMS[1]] + m.hfa)
    assert m.predict(TEAMS[0], TEAMS[1], neutral=True) == pytest.approx(
        -m.predict(TEAMS[1], TEAMS[0], neutral=True))
    assert 30 < m.predict_total(TEAMS[0], TEAMS[1]) < 60


def test_nfl_no_leakage():
    sched, _ = _nfl_schedule()
    asof = date(2025, 11, 1)
    epa = pl.DataFrame([{"season": 2025, "week": w, "team": t, "off_epa": 0.01 * i, "def_epa": 0.0}
                        for i, t in enumerate(TEAMS) for w in range(1, 17)])
    base = NflRatings.fit(sched, epa, asof)
    future = pl.col("gameday") >= asof
    tampered = sched.with_columns(
        pl.when(future).then(pl.col("home_score") + 40).otherwise(pl.col("home_score")).alias("home_score"),
        pl.when(future).then(0).otherwise(pl.col("away_score")).alias("away_score"))
    epa2 = epa.with_columns(pl.when(pl.col("week") >= 10).then(5.0).otherwise(pl.col("off_epa")).alias("off_epa"))
    other = NflRatings.fit(tampered, epa2, asof)
    for h, a in [(TEAMS[0], TEAMS[1]), (TEAMS[5], TEAMS[9])]:
        assert other.predict(h, a) == pytest.approx(base.predict(h, a))
        assert other.predict_total(h, a) == pytest.approx(base.predict_total(h, a))


def test_nfl_epa_blends():
    sched, _ = _nfl_schedule()
    epa = pl.DataFrame([{"season": 2025, "week": w, "team": t, "off_epa": 0.1 if t == "T00" else 0.0,
                         "def_epa": 0.0} for t in TEAMS for w in range(1, 17)])
    a = NflRatings.fit(sched, None, date(2025, 12, 31))
    b = NflRatings.fit(sched, epa, date(2025, 12, 31))
    assert b.ratings["T00"] != pytest.approx(a.ratings["T00"])


def _cfb_games(seed=0):
    rng = np.random.default_rng(seed)
    teams = [f"C{i:02d}" for i in range(40)]
    true = rng.normal(0, 10, 40)
    games = []
    for wk in range(1, 13):
        order = rng.permutation(40)
        for k in range(20):
            h, a = order[2 * k], order[2 * k + 1]
            margin = true[h] - true[a] + 2.5 + rng.normal(0, 14)
            games.append({"homeTeam": teams[h], "awayTeam": teams[a], "week": wk,
                          "startDate": (date(2025, 8, 30) + timedelta(days=7 * (wk - 1))).isoformat() + "T19:00:00.000Z",
                          "homePoints": int(round(30 + margin / 2)), "awayPoints": int(round(30 - margin / 2)),
                          "neutralSite": False})
    priors = {t: float(true[i] + rng.normal(0, 4)) for i, t in enumerate(teams)}
    return teams, true, games, priors


def test_cfb_shrinks_to_prior_early():
    teams, true, games, priors = _cfb_games()
    early = [g for g in games if g["week"] == 1]
    m = CfbRatings.fit(early, priors, date(2025, 9, 6))
    assert all(abs(m.ratings[t] - priors[t]) < 3 for t in teams)
    m12 = CfbRatings.fit(games, priors, date(2025, 11, 25))
    fitted = np.array([m12.ratings[t] for t in teams])
    assert np.corrcoef(fitted, true)[0, 1] > 0.7


def test_cfb_neutral_antisymmetric():
    teams, _, games, priors = _cfb_games()
    m = CfbRatings.fit(games, priors, date(2025, 11, 25))
    assert m.predict(teams[0], teams[1], True) == pytest.approx(-m.predict(teams[1], teams[0], True))


def test_cfb_unknown_team_prior_and_no_leakage():
    teams, _, games, priors = _cfb_games()
    asof = date(2025, 10, 4)
    base = CfbRatings.fit(games, priors, asof)
    tampered = [dict(g, homePoints=99, awayPoints=0) if g["week"] >= 6 else g for g in games]
    other = CfbRatings.fit(tampered, priors, asof)
    assert other.predict(teams[0], teams[1], False) == pytest.approx(base.predict(teams[0], teams[1], False))
    assert base.predict(teams[0], "NOBODY", True) > base.predict(teams[0], teams[1], True)


def test_cfb_prior_formula():
    out = cfb_prior({"A": 10.0, "B": -4.0}, {"A": 0.8}, {"A": 900.0, "B": 300.0})
    z = (900 - 600) / 300  # population std of [900, 300] is 300
    assert out["A"] == pytest.approx(0.6 * 10 + 0.25 * 10 * 0.3 + 0.15 * z * 7)
    assert out["B"] == pytest.approx(0.6 * -4 + 0.25 * -4 * 0.0 + 0.15 * -1 * 7)
    assert cfb_prior({"C": 5.0}, {}, {})["C"] == pytest.approx(3.0)
