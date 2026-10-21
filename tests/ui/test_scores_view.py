from dataclasses import replace
from datetime import datetime, timedelta, timezone

from gridiron_edge import describe as d
from gridiron_edge.types import Game, KalshiMarket, Quote
from gridiron_edge.ui import scores as sv

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)  # Sun 10:00 AM PT
PHI = Game("nfl:20261025:DAL@PHI", "nfl", 2026, 8, NOW + timedelta(hours=3, minutes=25), "PHI", "DAL")
CHI = Game("nfl:20261026:NE@CHI", "nfl", 2026, 8, NOW + timedelta(days=1), "CHI", "NE")
LIVE = Game("nfl:20261025:KC@BUF", "nfl", 2026, 8, NOW - timedelta(hours=1), "BUF", "KC", status="live",
            home_pts=10, away_pts=14, period="2", clock="4:12")
NAMES = d.Names({("cfb", "ALA"): "Alabama", ("cfb", "AUB"): "Auburn"})


def q(gid, market, side, point, book="dk", dec=1.91):
    return Quote("odds_api", book, gid, market, side, point, dec, NOW, NOW)


def mkt(gid, team, ask, status="active", mtype="ml"):
    return KalshiMarket(f"T-{gid}-{team}", "E", "S", mtype, gid, team, None, None, None, ask - 0.01, ask,
                        1 - ask, 1 - ask + 0.01, 100, 100, status, "", 0.07, NOW)


def test_book_lines_middle_of_the_books():
    quotes = [q(PHI.game_id, "spreads", "home", -3.5), q(PHI.game_id, "spreads", "away", 3.5),
              q(PHI.game_id, "spreads", "home", -3.0, book="fd"), q(PHI.game_id, "spreads", "home", -4.0, book="mgm"),
              q(PHI.game_id, "totals", "over", 44.5), q(PHI.game_id, "totals", "under", 44.5),
              q(CHI.game_id, "totals", "over", 40.0)]
    assert sv.book_lines(quotes, PHI) == "Eagles by 3.5 · total 44.5 points"
    assert sv.book_lines([q(PHI.game_id, "spreads", "home", 2.5)], PHI) == "Cowboys by 2.5"
    assert sv.book_lines([q(PHI.game_id, "spreads", "home", 0.0)], PHI) == "even game (no favorite)"
    assert sv.book_lines(quotes, LIVE) is None


def test_kalshi_prices_and_text():
    markets = [mkt(PHI.game_id, "PHI", 0.62), mkt(PHI.game_id, "DAL", 0.40), mkt(PHI.game_id, "XYZ", 0.5),
               mkt(PHI.game_id, "PHI", 0.3, mtype="spread"), mkt(CHI.game_id, "CHI", 0.55, status="closed")]
    p = sv.kalshi_prices(markets, PHI)
    assert p == {"PHI": 62, "DAL": 40}
    assert sv.kalshi_text(p, PHI) == "Kalshi win price: Cowboys 40 cents, Eagles 62 cents"
    assert sv.kalshi_text({"PHI": 1}, PHI) == "Kalshi win price: Eagles 1 cent"
    assert sv.kalshi_prices(markets, CHI) == {}
    assert sv.kalshi_text({}, CHI) is None


def test_chance_shows_the_likelier_team():
    assert sv.chance_text(("PHI", 0.61), PHI) == "our win chance: Eagles 61%"
    assert sv.chance_text(("PHI", 0.30), PHI) == "our win chance: Cowboys 70%"
    assert sv.chance_text(None, PHI) is None


def test_state_text():
    assert sv.state_text(PHI, NOW) == "kickoff Sun 1:25 PM PT"
    assert sv.state_text(LIVE, NOW) == "LIVE Chiefs 14, Bills 10 · 2nd quarter, 4:12 left"
    assert sv.state_text(replace(LIVE, period=None), NOW) == "LIVE Chiefs 14, Bills 10 · in progress"
    assert sv.state_text(replace(LIVE, status="final"), NOW) == "Final: Chiefs 14, Bills 10"
    assert sv.state_text(replace(PHI, status="postponed"), NOW) == "Postponed"


def test_rows_grouped_by_league_and_day_with_bets_and_prices():
    cfb_a = Game("cfb:20261025:AUB@ALA", "cfb", 2026, 9, NOW + timedelta(hours=5), "ALA", "AUB")
    cfb_plain = [Game(f"cfb:20261025:A{i}@B{i}", "cfb", 2026, 9, NOW + timedelta(hours=2, minutes=i), f"B{i}",
                      f"A{i}") for i in range(4)]
    games = [CHI, cfb_a, PHI, LIVE, *cfb_plain]
    rows = sv.score_rows(games, NOW, quotes=[q(PHI.game_id, "spreads", "home", -3.5)],
                         markets=[mkt(PHI.game_id, "PHI", 0.62), mkt(cfb_a.game_id, "ALA", 0.8)],
                         chances={PHI.game_id: ("PHI", 0.64)}, bet_counts={PHI.game_id: 2},
                         bet_status={LIVE.game_id: "winning"}, names=NAMES, cfb_extra_limit=2)
    heads = [r.head for r in rows]
    assert heads == ["NFL · Today (2 games)", "Chiefs at Bills", "Cowboys at Eagles",
                     "NFL · Tomorrow (1 game)", "Patriots at Bears",
                     "College football · Today (3 games)", "Auburn at Alabama", "A0 at B0", "A1 at B1",
                     "2 more college games with no Kalshi prices and none of your bets (not shown)"]
    phi = rows[2]
    assert phi.details == ["Eagles by 3.5", "Kalshi win price: Eagles 62 cents", "our win chance: Eagles 64%",
                           "your bets: 2"]
    assert rows[1].details == ["your bet is winning right now"] and rows[1].bet_status == "winning"
    assert rows[-1].note and rows[0].game is None and not rows[0].note


def test_bet_counts_once_per_game():
    bets = [{"legs": [{"game_id": "g1"}, {"game_id": "g1"}]}, {"legs": [{"game_id": "g1"}, {"game_id": "g2"}]},
            {"legs": [{"game_id": None}]}]
    assert sv.bet_counts(bets, lambda b: b["legs"]) == {"g1": 2, "g2": 1}


def test_finished_games_go_last_within_the_day():
    early_final = replace(LIVE, game_id="nfl:f", status="final", kickoff=NOW - timedelta(hours=4), home="NO",
                          away="JAX")
    rows = sv.score_rows([early_final, PHI, LIVE], NOW)
    assert [r.head for r in rows] == ["NFL · Today (3 games)", "Chiefs at Bills", "Cowboys at Eagles",
                                      "Jaguars at Saints"]
