"""Neutral-site games get no home-field advantage, in training and at prediction time."""
from datetime import date, timedelta

import numpy as np
import polars as pl

from gridiron_edge.clients.nflverse import SCHEDULE_COLS
from gridiron_edge.models.nfl import NflRatings
from gridiron_edge.services import _nfl_history

TEAMS = [f"T{i:02d}" for i in range(32)]


def _sched(label_neutral: bool):
    rng = np.random.default_rng(1)
    true = rng.normal(0, 6, 32)
    rows = []
    for wk in range(17):
        order = rng.permutation(32)
        for k in range(16):
            h, a = order[2 * k], order[2 * k + 1]
            neutral = k < 4  # a quarter of the games: listed home team gets no edge
            margin = true[h] - true[a] + (0.0 if neutral else 2.0) + rng.normal(0, 3)
            if neutral:
                margin -= 6.0  # the listed "home" side does worse at these sites
            rows.append({"season": 2025, "week": wk + 1, "gameday": date(2025, 9, 7) + timedelta(days=7 * wk),
                         "home_team": TEAMS[h], "away_team": TEAMS[a],
                         "home_score": int(round(24 + margin / 2)), "away_score": int(round(24 - margin / 2)),
                         "result": float(round(margin)), "spread_line": 0.0,
                         "location": "Neutral" if (neutral and label_neutral) else "Home"})
    return pl.DataFrame(rows)


def test_schedule_cols_include_location():
    assert "location" in SCHEDULE_COLS


def test_nfl_fit_reads_location_neutral():
    labelled = NflRatings.fit(_sched(True), None, date(2026, 2, 1))
    unlabelled = NflRatings.fit(_sched(False), None, date(2026, 2, 1))
    assert abs(labelled.hfa - 2.0) < 0.6
    assert unlabelled.hfa < labelled.hfa - 0.5


def test_nfl_history_carries_neutral():
    rows = _nfl_history(_sched(True), date(2026, 2, 1))
    assert sum(r["neutral"] for r in rows) == 4 * 17
