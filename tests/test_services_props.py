"""Services prop plumbing: fetch-time stamps, persistence, name matching, projector gate."""
from datetime import UTC, datetime, timedelta

import polars as pl

from gridiron_edge.types import PropQuote

NOW = datetime(2026, 10, 31, 18, 5, tzinfo=UTC)


def test_merged_props_are_stamped_with_fetch_time_and_persist(services_mocked):
    svc = services_mocked()
    old = NOW - timedelta(hours=3)
    q = PropQuote("odds_api", "draftkings", "g1", "A.J. Brown", "reception_yds", "over", 70.5, 1.9, old)
    svc._merge("props", "odds_api", [q], NOW)
    assert svc.latest["props"][0].fetched_at == NOW
    svc.latest["props"] = []
    svc.load_latest(NOW + timedelta(hours=1))
    assert svc.latest["props"][0].fetched_at == NOW and svc.latest["props"][0].last_update == old


def test_prop_ttl_runs_from_fetch_time(services_mocked):
    svc = services_mocked()
    ancient = NOW - timedelta(days=3)  # book stamp older than the 2-day TTL, but fetched just now
    q = PropQuote("odds_api", "draftkings", "g1", "X", "rush_yds", "over", 50.5, 1.9, ancient)
    svc._merge("props", "odds_api", [q], NOW)
    svc._merge("props", "odds_api", [], NOW + timedelta(hours=1))
    assert len(svc.latest["props"]) == 1


def test_stat_lookup_matches_normalised_names(services_mocked):
    from gridiron_edge.types import Game
    svc = services_mocked()
    g = Game("nfl:20261025:DAL@PHI", "nfl", 2026, 8, NOW - timedelta(days=6), "PHI", "DAL", status="final")
    svc.registry.get = lambda gid: g if gid == g.game_id else None
    svc.latest["stats"] = pl.DataFrame({"player_display_name": ["AJ Brown"], "season": [2026], "week": [8],
                                        "recent_team": ["PHI"], "receiving_yards": [88.0]})
    assert svc._stat_lookup(g.game_id, "A.J. Brown Jr.", "reception_yds") == 88.0


def test_snapshot_open_players_and_projector_flag(services_mocked):
    svc = services_mocked()
    snap = svc.snapshot(NOW)
    assert snap.projector_live is False


async def test_nflverse_load_builds_projector_with_snaps_and_injuries_and_gate(services_mocked):
    from gridiron_edge.models.props import PropProjector
    svc = services_mocked()
    calls = []

    class Extra:
        def snap_counts(self, seasons):
            calls.append("snaps")
            return pl.DataFrame({"player": ["A"], "season": [2026], "week": [1], "offense_pct": [0.9]})

        def injuries(self, seasons):
            calls.append("injuries")
            return pl.DataFrame({"full_name": ["A"], "season": [2026], "week": [9], "report_status": ["Out"]})

    fake = svc.nflverse
    svc.nflverse = type("N", (), {"schedules": fake.schedules, "weekly_player_stats": fake.weekly_player_stats,
                                  "team_epa": fake.team_epa, "snap_counts": Extra().snap_counts,
                                  "injuries": Extra().injuries})()
    out = await svc._load_nflverse(NOW)
    assert calls == ["snaps", "injuries"] and isinstance(svc.projector, PropProjector)
    assert svc.projector.is_out("A", 2026, 9)
    assert svc.projector_live is False and svc.snapshot(NOW).projector_live is False
    assert any("player-prop model" in m for _, m in out)


async def test_nflverse_load_survives_missing_snaps_and_injuries(services_mocked):
    svc = services_mocked()  # FakeNflverse has neither
    out = await svc._load_nflverse(NOW)
    assert svc.projector_live is False
    assert any("injury" in m for _, m in out)


async def test_snaps_and_injuries_can_be_turned_off(services_mocked):
    svc = services_mocked(props_use_snaps_injuries=False)
    svc.nflverse.snap_counts = lambda seasons: (_ for _ in ()).throw(AssertionError("should not load"))
    await svc._load_nflverse(NOW)


async def test_scan_logs_new_unmatched_player_names_once(services_mocked):
    from gridiron_edge.scheduler import Job
    svc = services_mocked()

    class P:
        unmatched = {"Mystery Man"}

        def project(self, *a, **k):
            return None

        def team_of(self, player):
            return None

    svc.projector = P()
    out = await svc._job_scan(Job("scan", None, {"what": "all"}), NOW)
    assert any("Mystery Man" in m for _, m in out)
    out = await svc._job_scan(Job("scan", None, {"what": "all"}), NOW)
    assert not any("Mystery Man" in m for _, m in out)
