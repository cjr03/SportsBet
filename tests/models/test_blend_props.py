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


def _wf_games(n=400, seed=5):
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        true = rng.normal(0, 7)
        line = round(true * 0.6 * 2) / 2
        out.append({"date": date(2026, 9, 1) + timedelta(days=k // 12), "home": "A", "away": "B",
                    "neutral": False, "home_line": -line, "margin": round(true + rng.normal(0, 13))})
    return out


def test_walkforward_reports_baselines_and_reliability():
    fit, m = walkforward_ats("nfl", _wf_games(), lambda d: (lambda g: -g["home_line"] / 0.6))
    assert m["brier_baseline"] == 0.25 and m["logloss_baseline"] == pytest.approx(np.log(2))
    assert m["breakeven"] == pytest.approx(110 / 210)
    assert m["bss"] == pytest.approx(1 - m["brier"] / 0.25)
    rel = m["reliability"]
    assert len(rel) == 5 and sum(b["n"] for b in rel) == m["n"]
    assert all(b["n"] == 0 or 0 <= b["hit_rate"] <= 1 for b in rel)


def test_walkforward_drops_dead_market_column_and_skips_burn_in():
    games = _wf_games()
    first_week = min(g["date"] for g in games)
    first_week -= timedelta(days=first_week.weekday())

    def predict_asof(d):
        if d == first_week:
            return lambda g: None  # nothing fitted yet: no forecast, not a 0-margin underdog pick
        return lambda g: -g["home_line"] / 0.6

    fit, m = walkforward_ats("nfl", games, predict_asof)
    full, _ = walkforward_ats("nfl", games, lambda d: (lambda g: -g["home_line"] / 0.6))
    assert fit.b_market == 0.0 and fit.n < full.n
    assert m["skipped_burn_in"] > 0
