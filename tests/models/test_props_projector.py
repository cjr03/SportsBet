"""Projector upgrades: shrinkage, pooled dispersion, snaps/injuries, team change, usage x efficiency,
opponent and game-script factors, calibration gate."""
import numpy as np
import polars as pl
import pytest

from gridiron_edge.models.props import PropProjector


def _league(seed=0, seasons=(2025,), players=12, weeks=17, extra=None):
    """Synthetic RB league: each player has a true rate; one row per player-week."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(players):
        rate = 4.0 + 0.1 * i
        carries_mu = 10 + i
        for season in seasons:
            for wk in range(1, weeks + 1):
                c = int(rng.poisson(carries_mu))
                rows.append({"player_display_name": f"Back {i}", "position": "RB", "season": season,
                             "week": wk, "recent_team": f"T{i}", "opponent_team": f"T{(i + wk) % players}",
                             "carries": float(c), "rushing_yards": float(max(0.0, rng.normal(rate * c, 15)))})
    rows += extra or []
    return pl.DataFrame(rows)


def test_mean_shrinks_toward_position_prior_without_usage():
    df = _league().drop("carries")
    hot = [{"player_display_name": "Hot Hand", "position": "RB", "season": 2026, "week": w, "recent_team": "X",
            "opponent_team": "T1", "rushing_yards": 200.0} for w in range(1, 5)]
    p = PropProjector(pl.concat([df, pl.DataFrame(hot)], how="diagonal_relaxed"))
    mean, _ = p.project("Hot Hand", "rush_yds", 2026, 5)
    assert 60 < mean < 200  # pulled toward the RB average, not left at the 4-game streak


def test_sd_shrinks_toward_pooled_cv():
    df = _league().drop("carries")
    flat = [{"player_display_name": "Metronome", "position": "RB", "season": 2026, "week": w, "recent_team": "X",
             "opponent_team": "T1", "rushing_yards": 70.0 + (w % 2)} for w in range(1, 6)]
    p = PropProjector(pl.concat([df, pl.DataFrame(flat)], how="diagonal_relaxed"))
    mean, sd = p.project("Metronome", "rush_yds", 2026, 6)
    assert sd / mean > 0.1  # five near-identical games don't make a near-certain projection


def test_low_snap_games_dropped_and_out_players_skipped():
    rows = [{"player_display_name": "Part Timer", "season": 2026, "week": w, "recent_team": "KC",
             "rushing_yards": y} for w, y in enumerate([80, 85, 5, 90, 75, 4], start=1)]
    df = pl.DataFrame(rows)
    snaps = pl.DataFrame({"player": ["Part Timer"] * 6, "season": [2026] * 6, "week": list(range(1, 7)),
                          "offense_pct": [0.8, 0.75, 0.1, 0.7, 0.8, 0.15]})
    with_snaps = PropProjector(df, snaps=snaps).project("Part Timer", "rush_yds", 2026, 7)
    without = PropProjector(df).project("Part Timer", "rush_yds", 2026, 7)
    assert with_snaps[0] > without[0] + 10
    inj = pl.DataFrame({"full_name": ["Part Timer"], "season": [2026], "week": [7], "report_status": ["Out"]})
    p = PropProjector(df, snaps=snaps, injuries=inj)
    assert p.project("Part Timer", "rush_yds", 2026, 7) is None and p.is_out("Part Timer", 2026, 7)
    q = PropProjector(df, injuries=inj.with_columns(pl.lit("Questionable").alias("report_status")))
    assert q.project("Part Timer", "rush_yds", 2026, 7) is not None


def test_games_for_a_previous_team_count_less():
    rows = ([{"player_display_name": "Mover", "season": 2026, "week": w, "recent_team": "OLD",
              "rushing_yards": 120.0} for w in range(1, 7)]
            + [{"player_display_name": "Mover", "season": 2026, "week": w, "recent_team": "NEW",
                "rushing_yards": 40.0} for w in range(7, 9)])
    mean, _ = PropProjector(pl.DataFrame(rows)).project("Mover", "rush_yds", 2026, 9)
    same = [dict(r, recent_team="NEW") for r in rows]
    mean_same, _ = PropProjector(pl.DataFrame(same)).project("Mover", "rush_yds", 2026, 9)
    assert mean < mean_same


def test_usage_times_efficiency_shrinks_efficiency_to_league():
    df = _league(seasons=(2025, 2026), weeks=8)
    p = PropProjector(df)
    mean, sd = p.project("Back 5", "rush_yds", 2026, 9)
    hist = df.filter((pl.col("player_display_name") == "Back 5") & (pl.col("season") == 2026))
    assert mean == pytest.approx(float(hist["rushing_yards"].mean()), rel=0.2)
    off = PropProjector(df, usage_model=False).project("Back 5", "rush_yds", 2026, 9)
    assert off[0] != pytest.approx(mean)


def test_opponent_and_game_script_factors():
    df = _league(seasons=(2025, 2026), weeks=8)
    leaky = [{"player_display_name": f"Gash {w}", "season": 2026, "week": w, "recent_team": "ZZ",
              "opponent_team": "SIEVE", "carries": 20.0, "rushing_yards": 220.0} for w in range(1, 9)]
    p = PropProjector(pl.concat([df, pl.DataFrame(leaky)], how="diagonal_relaxed"))
    base, _ = p.project("Back 3", "rush_yds", 2026, 9)
    vs_sieve, _ = p.project("Back 3", "rush_yds", 2026, 9, opponent="SIEVE")
    assert vs_sieve > base
    fav, _ = p.project("Back 3", "rush_yds", 2026, 9, team_total=27.0, team_spread=-7.0)
    dog, _ = p.project("Back 3", "rush_yds", 2026, 9, team_total=17.0, team_spread=7.0)
    assert fav > base > dog
    assert PropProjector.script_factor("passing_yards", 22.0, -10.0) < 1.0  # favourites throw less


def test_calibration_backtest_reports_and_needs_volume():
    p = PropProjector(_league(seasons=(2024, 2025), weeks=17))
    res = p.calibration_backtest(2025, 0.035)
    assert set(res) >= {"n", "claimed", "hit", "live"} and res["n"] < 300 and res["live"] is False
    assert PropProjector(_league().head(0)).calibration_backtest(2025, 0.035)["n"] == 0
