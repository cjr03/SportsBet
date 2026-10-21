from datetime import date, datetime, timedelta, timezone

import pytest

from gridiron_edge.matching import (GameRegistry, TeamResolver, kalshi_resolver,
                                    parse_kalshi_event)
from gridiron_edge.types import Game


@pytest.fixture
def resolver(conn):
    return TeamResolver(conn)


@pytest.fixture
def registry(conn, resolver):
    return GameRegistry(conn, resolver)


def test_canonical_variants(resolver):
    for n in ["Kansas City Chiefs", "chiefs", "KC", "Kansas City"]:
        assert resolver.canonical("nfl", n) == "KC"
    assert resolver.canonical("nfl", "Springfield Isotopes") is None


def test_all_32_teams_seeded(resolver):
    import json
    from pathlib import Path
    import gridiron_edge.matching as m
    teams = json.loads((Path(m.__file__).parent / "data" / "nfl_teams.json").read_text())
    assert len(teams) == 32
    for abbr, info in teams.items():
        for n in info["names"] + info["kalshi"]:
            assert resolver.canonical("nfl", n) == abbr


def test_parse_kalshi_event():
    assert parse_kalshi_event("KXNFLGAME-26NOV02CHISEA") == (date(2026, 11, 2), "CHI", "SEA")
    assert parse_kalshi_event("garbage") is None
    assert parse_kalshi_event("KXNFLGAME-26XXX02CHISEA") is None


def test_parse_prefers_known_split():
    # 6 letters: 3+3 by default, but known codes steer to 2+4 / 4+2
    assert parse_kalshi_event("KXNCAAFGAME-26OCT24ABCDEF") == (date(2026, 10, 24), "ABC", "DEF")
    assert parse_kalshi_event("KXNCAAFGAME-26OCT24ABCDEF", {"AB", "CDEF"}) == (
        date(2026, 10, 24), "AB", "CDEF")
    assert parse_kalshi_event("KXNCAAFGAME-26OCT24ABCDEF", {"ABCD", "EF"}) == (
        date(2026, 10, 24), "ABCD", "EF")


def test_resolve_game_window(registry, now):
    g = Game("nfl:20261102:CHI@SEA", "nfl", 2026, 9, now, "SEA", "CHI")
    registry.upsert(g)
    assert registry.resolve("nfl", "Seattle Seahawks", "Chicago Bears",
                            now + timedelta(hours=2)) == g.game_id
    assert registry.resolve("nfl", "Seattle Seahawks", "Chicago Bears",
                            now + timedelta(days=5)) is None
    assert registry.resolve("nfl", "Chicago Bears", "Seattle Seahawks", now) is None
    assert registry.resolve("nfl", "Springfield Isotopes", "Chicago Bears", now) is None


def test_neutral_site_swapped(registry, now):
    registry.teams.add("cfb", "Alpha", "A")
    registry.teams.add("cfb", "Beta", "B")
    g = Game("cfb:20261024:A@B", "cfb", 2026, 8, now, "B", "A", neutral=True)
    registry.upsert(g)
    assert registry.resolve("cfb", "Alpha", "Beta", now) == g.game_id


def test_upsert_get_roundtrip_and_preserves(registry, now):
    g = Game("nfl:20261102:CHI@SEA", "nfl", 2026, 9, now, "SEA", "CHI")
    registry.upsert(g)
    assert registry.get(g.game_id) == g
    assert registry.get("nope") is None
    registry.set_odds_event(g.game_id, "ev1")
    registry.update_score(g.game_id, "live", 7, 3, "Q2", "05:00")
    registry.upsert(g)  # schedule refresh must not erase live data
    got = registry.get(g.game_id)
    assert (got.status, got.home_pts, got.away_pts, got.period) == ("live", 7, 3, "Q2")
    row = registry.conn.execute("SELECT odds_event_id FROM games").fetchone()
    assert row[0] == "ev1"


def test_upcoming_live_all_since(registry, now):
    soon = Game("nfl:a", "nfl", 2026, 1, now + timedelta(hours=3), "SEA", "CHI")
    later = Game("nfl:b", "nfl", 2026, 1, now + timedelta(days=3), "KC", "DEN")
    old = Game("nfl:c", "nfl", 2026, 1, now - timedelta(days=2), "GB", "MIN", status="final")
    for g in (soon, later, old):
        registry.upsert(g)
    assert [g.game_id for g in registry.upcoming(timedelta(hours=24), now)] == ["nfl:a"]
    registry.update_score("nfl:a", "live", 0, 0, "Q1", "15:00")
    assert [g.game_id for g in registry.live()] == ["nfl:a"]
    assert [g.game_id for g in registry.all_since(now)] == ["nfl:a", "nfl:b"]


def test_kalshi_resolver(registry):
    ko = datetime(2026, 11, 3, 1, 15, tzinfo=timezone.utc)  # Nov 2 evening local
    g = Game("nfl:20261102:CHI@SEA", "nfl", 2026, 9, ko, "SEA", "CHI")
    registry.upsert(g)
    r = kalshi_resolver(registry, registry.teams)
    assert r({"event_ticker": "KXNFLGAME-26NOV02CHISEA", "yes_sub_title": "Seattle"}) == (
        g.game_id, "SEA")
    assert r({"event_ticker": "KXNFLTOTAL-26NOV02CHISEA",
              "yes_sub_title": "Over 44.5 points scored"}) == (g.game_id, None)
    assert r({"event_ticker": "KXNFLGAME-26NOV02ZZZYYY", "yes_sub_title": "x"}) == (None, None)
    assert r({"event_ticker": "garbage", "yes_sub_title": ""}) == (None, None)


def test_display_names_for_cfb(resolver):
    resolver.set_display("cfb", "OSU", "Ohio State")
    resolver.set_display("cfb", "OSU", "Ohio State")
    assert resolver.display_names() == {("cfb", "OSU"): "Ohio State"}
