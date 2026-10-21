"""QB-change veto: depth-chart QB1 differs from the team's last starter."""
from datetime import date

import polars as pl

from gridiron_edge.clients.nflverse import qb1_from_depth_chart
from gridiron_edge.models.nfl import qb_changes


def test_qb1_new_schema_latest_snapshot():
    df = pl.DataFrame([
        {"dt": "2026-10-20T07:00:00Z", "team": "KC", "pos_abb": "QB", "pos_rank": 1, "gsis_id": "old"},
        {"dt": "2026-10-27T07:00:00Z", "team": "KC", "pos_abb": "QB", "pos_rank": 1, "gsis_id": "new"},
        {"dt": "2026-10-27T07:00:00Z", "team": "KC", "pos_abb": "QB", "pos_rank": 2, "gsis_id": "bk"},
        {"dt": "2026-10-27T07:00:00Z", "team": "KC", "pos_abb": "WR", "pos_rank": 1, "gsis_id": "wr"},
    ])
    assert qb1_from_depth_chart(df) == {"KC": "new"}


def test_qb1_old_schema():
    df = pl.DataFrame([
        {"season": 2024, "week": 7, "club_code": "BUF", "position": "QB", "depth_team": "1", "gsis_id": "a"},
        {"season": 2024, "week": 8, "club_code": "BUF", "position": "QB", "depth_team": "1", "gsis_id": "b"},
        {"season": 2024, "week": 8, "club_code": "BUF", "position": "QB", "depth_team": "2", "gsis_id": "c"},
    ])
    assert qb1_from_depth_chart(df) == {"BUF": "b"}
    assert qb1_from_depth_chart(pl.DataFrame()) == {}


def test_qb_changes_against_last_started_game():
    sched = pl.DataFrame([
        {"gameday": date(2026, 10, 19), "home_team": "KC", "away_team": "BUF", "home_score": 20,
         "away_score": 17, "home_qb_id": "mahomes", "away_qb_id": "allen"},
        {"gameday": date(2026, 10, 26), "home_team": "DEN", "away_team": "KC", "home_score": 10,
         "away_score": 27, "home_qb_id": "nix", "away_qb_id": "mahomes"},
        {"gameday": date(2026, 11, 2), "home_team": "KC", "away_team": "DEN", "home_score": None,
         "away_score": None, "home_qb_id": None, "away_qb_id": None},
    ])
    qb1 = {"KC": "backup", "BUF": "allen", "DEN": "nix", "NYJ": "x"}
    assert qb_changes(sched, qb1, date(2026, 10, 30)) == {"KC"}
