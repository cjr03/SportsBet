from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from gridiron_edge.models.blend import blend, default_weight, fit_blend, walkforward_ats
from gridiron_edge.models.props import PropProjector


@pytest.fixture
def weekly_df():
    rows = []
    for wk, yds in enumerate([60, 90, 70, 80, 65, 85, 75, 72], start=1):
        rows.append({"player_display_name": "Vet Back", "season": 2026, "week": wk,
                     "recent_team": "KC", "rushing_yards": float(yds),
                     "receiving_yards": 10.0, "passing_yards": 0.0, "receptions": 1.0})
    for wk in (1, 2):
        rows.append({"player_display_name": "Rookie Guy", "season": 2026, "week": wk,
                     "recent_team": "DEN", "rushing_yards": 30.0,
                     "receiving_yards": 0.0, "passing_yards": 0.0, "receptions": 0.0})
    return pl.DataFrame(rows)


def test_blend_weights():
    assert blend(0.6, 0.5, 0.0) == pytest.approx(0.5)
    assert blend(0.6, 0.5, 1.0) == pytest.approx(0.6)


def test_gate_on_useless_model():
    rng = np.random.default_rng(0)
    pm = rng.uniform(.3, .7, 2000)
    y = rng.binomial(1, pm)
    assert fit_blend(rng.uniform(.3, .7, 2000), pm, y).gated


def test_no_gate_on_informative_model():
    rng = np.random.default_rng(1)
    truth = rng.uniform(.3, .7, 3000)
    y = rng.binomial(1, truth)
    market = np.clip(truth + rng.normal(0, .08, 3000), .05, .95)
    assert not fit_blend(truth, market, y).gated


def test_gate_on_small_sample():
    rng = np.random.default_rng(2)
    truth = rng.uniform(.2, .8, 100)
    assert fit_blend(truth, truth, rng.binomial(1, truth)).gated


def test_default_weight():
    assert default_weight("nfl", 48) == 0.25
    assert default_weight("cfb", 2) == 0.125
    assert default_weight("cfb", 48) == 0.35
    assert default_weight("nfl", 24) == 0.125


def test_walkforward_ats():
    rng = np.random.default_rng(3)
    games = []
    for k in range(600):
        true = rng.normal(0, 7)
        line = round(true * 0.6 * 2) / 2
        games.append({"date": date(2026, 9, 1) + timedelta(days=k // 12), "home": "A",
                      "away": "B", "neutral": False, "home_line": -line,
                      "margin": round(true + rng.normal(0, 13))})
    asofs = []

    def predict_asof(d):
        asofs.append(d)
        return lambda g: -g["home_line"] / 0.6

    fit, m = walkforward_ats("nfl", games, predict_asof)
    assert m["n"] == fit.n <= 600
    assert 0.0 <= m["ats_pct"] <= 1.0 and 0 < m["brier"] < 0.5 and m["logloss"] > 0
    assert len(set(asofs)) > 5 and asofs == sorted(asofs)


def test_walkforward_excludes_pushes():
    games = [{"date": date(2026, 9, 1), "home": "A", "away": "B", "neutral": False,
              "home_line": -3.0, "margin": 3}]
    fit, m = walkforward_ats("nfl", games, lambda d: (lambda g: 3.0))
    assert m["n"] == 0


def test_projector_needs_4_games(weekly_df):
    assert PropProjector(weekly_df).project("Rookie Guy", "rush_yds", 2026, 3) is None


def test_projector_mean(weekly_df):
    m, sd = PropProjector(weekly_df).project("vet back", "rush_yds", 2026, 9)
    assert 60 < m < 90 and sd > 0


def test_projector_asof_excludes_future(weekly_df):
    assert PropProjector(weekly_df).project("Vet Back", "rush_yds", 2026, 4) is None


def test_projector_unknown_player_and_stat(weekly_df):
    p = PropProjector(weekly_df)
    assert p.project("Nobody", "rush_yds", 2026, 9) is None
    assert p.project("Vet Back", "sacks", 2026, 9) is None


def test_team_of(weekly_df):
    p = PropProjector(weekly_df)
    assert p.team_of("VET back") == "KC"
    assert p.team_of("Nobody") is None


def test_walkforward_reports_disagreement_p95():
    games = [{"date": date(2026, 9, 1) + timedelta(days=k), "home": "A", "away": "B", "neutral": False,
              "home_line": -3.0, "margin": 10 if k % 2 else -10} for k in range(100)]
    _, m = walkforward_ats("nfl", games, lambda d: (lambda g: 3.0 + (d.day % 5)))
    assert 3.0 <= m["disagree_p95"] <= 4.0


def test_walkforward_totals_and_production_margin_model():
    from gridiron_edge.models.blend import walkforward_totals
    from gridiron_edge.strategy.distributions import NormalModel

    rng = np.random.default_rng(5)
    games = []
    for k in range(400):
        true = 44 + rng.normal(0, 5)
        games.append({"date": date(2025, 9, 1) + timedelta(days=k // 10), "season": 2025,
                      "total_line": round(true * 2) / 2 + 0.25, "total": round(true + rng.normal(0, 13))})
    fit, m = walkforward_totals(games, lambda d: (lambda g: g["total_line"] + 1.0))
    assert m["n"] == fit.n > 0 and set(m["by_season"]) == {"2025"} and "sim_roi" in m
    wide = walkforward_totals(games, lambda d: (lambda g: g["total_line"] + 1.0), NormalModel(40.0))[1]
    assert wide["brier"] != m["brier"]


def test_walkforward_early_late_split():
    games = [{"date": date(2025, 9, 1) + timedelta(days=7 * k), "season": 2025, "home_line": -3.0,
              "margin": 10 if k % 2 else -10} for k in range(10)]
    _, m = walkforward_ats("nfl", games, lambda d: (lambda g: 4.0))
    assert m["early"]["n"] == 4 and m["late"]["n"] == 6
