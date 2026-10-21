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


def test_normalize_name_strips_suffix_punctuation_accents_and_nicknames():
    from gridiron_edge.matching import normalize_name
    assert normalize_name("A.J. Brown") == normalize_name("AJ Brown") == "aj brown"
    assert normalize_name("Kenneth Walker III") == normalize_name("Ken Walker") == "kenneth walker"
    assert normalize_name("Marvin Harrison Jr.") == "marvin harrison"
    assert normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"
    assert normalize_name("Gabe Davis") == normalize_name("Gabriel Davis")
    assert normalize_name("José  Ramírez ") == "jose ramirez"


def test_display_names_for_cfb(resolver):
    resolver.set_display("cfb", "OSU", "Ohio State")
    resolver.set_display("cfb", "OSU", "Ohio State")
    assert resolver.display_names() == {("cfb", "OSU"): "Ohio State"}


# -- spread markets: the team comes from the ticker suffix, not the "X wins by over N" sentence ----

def test_kalshi_ticker_team_from_suffix():
    from gridiron_edge.matching import kalshi_ticker_team
    assert kalshi_ticker_team("KXNFLSPREAD-26OCT26DALPHI-PHI8", "DAL", "PHI") == "PHI"
    assert kalshi_ticker_team("KXNCAAFSPREAD-26OCT24AKRKENT-KENT7", "AKR", "KENT") == "KENT"
    assert kalshi_ticker_team("KXNCAAFSPREAD-26OCT24AKRKENT-AKR6", "AKR", "KENT") == "AKR"
    assert kalshi_ticker_team("KXNCAAFSPREAD-26OCT24COLOOKST-OKST8", "COLO", "OKST") == "OKST"
    assert kalshi_ticker_team("KXNFLGAME-26NOV02CHISEA-SEA", "CHI", "SEA") == "SEA"
    assert kalshi_ticker_team("KXNFLTOTAL-26NOV02CHISEA-45", "CHI", "SEA") is None
    assert kalshi_ticker_team("KXNFLSPREAD-26NOV02CHISEA-SEAX3", "CHI", "SEA") is None
    assert kalshi_ticker_team("KXNCAAFSPREAD-26OCT24OKOKST-OKST3", "OK", "OKST") == "OKST"  # longest code wins


def test_kalshi_resolver_spread_team_from_ticker(registry):
    ko = datetime(2026, 10, 27, 0, 20, tzinfo=timezone.utc)
    g = Game("nfl:20261026:DAL@PHI", "nfl", 2026, 8, ko, "PHI", "DAL")
    registry.upsert(g)
    r = kalshi_resolver(registry, registry.teams)
    assert r({"event_ticker": "KXNFLSPREAD-26OCT26DALPHI", "ticker": "KXNFLSPREAD-26OCT26DALPHI-PHI8",
              "yes_sub_title": "Philadelphia wins by over 7.5 points"}) == (g.game_id, "PHI")
    assert r({"event_ticker": "KXNFLSPREAD-26OCT26DALPHI", "ticker": "KXNFLSPREAD-26OCT26DALPHI-DAL3",
              "yes_sub_title": "Dallas wins by over 2.5 points"}) == (g.game_id, "DAL")
    assert r({"event_ticker": "KXNFLTOTAL-26OCT26DALPHI", "ticker": "KXNFLTOTAL-26OCT26DALPHI-45",
              "yes_sub_title": "Over 44.5 points"}) == (g.game_id, None)
    # no ticker: the sentence still resolves with "wins by over ..." stripped
    assert r({"event_ticker": "KXNFLSPREAD-26OCT26DALPHI",
              "yes_sub_title": "Philadelphia wins by over 7.5 points"}) == (g.game_id, "PHI")
