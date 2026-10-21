"""Evaluation wiring in Services: CLV recording, shadow CLV, drift, report."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gridiron_edge.types import Game, Leg, Quote, Recommendation

NOW = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
KICK = NOW + timedelta(minutes=20)


def spreads(gid, home_point, dec=1.95, book="pinnacle", ts=NOW):
    return [Quote("odds_api", book, gid, "spreads", "home", home_point, dec, ts),
            Quote("odds_api", book, gid, "spreads", "away", -home_point, dec, ts)]


def setup_game(svc, mk, league="cfb", gid="cfb:20261031:AAA@BBB", line=3.5, home_point=-7.0):
    svc.registry.upsert(Game(gid, league, 2026, 9, KICK, "BBB", "AAA"))
    m = mk("T-BBB", "E", "spread", "BBB", line=line, yes_ask=0.50, game_id=gid)
    svc.latest["kalshi"] = [m]
    svc.latest["quotes"] = spreads(gid, home_point)
    return gid, m


def rec_for(m, gid, observe=False, price=0.50, qty=10, p_mkt=None):
    leg = Leg("kalshi", "BBB wins by >3.5", "yes", price, 0.6, gid, ticker=m.ticker, team="BBB",
              line=m.line, p_mkt=p_mkt)
    return Recommendation(None, "kalshi_game", [leg], 0 if observe else 500, qty, 0.05, 0.6, None,
                          NOW - timedelta(hours=1), KICK, observe_only=observe, p_mkt=p_mkt)


def test_cfb_bet_gets_clv(services_mocked, mk):
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    gid, m = setup_game(svc, mk)
    rec, _ = svc.lifecycle.upsert_recommendation(rec_for(m, gid), NOW - timedelta(hours=1))
    bet = svc.lifecycle.confirm(rec.id, NOW - timedelta(minutes=50))
    out = svc._record_clv(NOW)
    clv = svc.conn.execute("SELECT clv FROM bets WHERE id=?", (bet,)).fetchone()[0]
    assert clv is not None and clv > 0  # home -7 line makes "wins by >3.5" likelier than 50 cents
    assert any(f"bet {bet}" in msg for _, msg in out)


def test_observe_and_unconfirmed_recs_get_shadow_clv(services_mocked, mk):
    svc = services_mocked()
    gid, m = setup_game(svc, mk)
    logged, _ = svc.lifecycle.upsert_recommendation(rec_for(m, gid, observe=True, p_mkt=0.55), NOW - timedelta(hours=1))
    other = replace(rec_for(m, gid, price=0.52), legs=[replace(rec_for(m, gid).legs[0], price=0.52)])
    unconfirmed, _ = svc.lifecycle.upsert_recommendation(other, NOW - timedelta(minutes=30))
    svc.lifecycle.skip(unconfirmed.id)
    svc._record_clv(NOW)
    rows = {r["id"]: r for r in svc.conn.execute("SELECT * FROM recommendations")}
    for rid in (logged.id, unconfirmed.id):
        assert rows[rid]["clv_close"] is not None and rows[rid]["p_close_fair"] is not None
        assert rows[rid]["clv_source"]
    assert rows[logged.id]["p_mkt"] == pytest.approx(0.55)
    # cheaper price, same close -> higher CLV
    assert rows[logged.id]["clv_close"] > rows[unconfirmed.id]["clv_close"]


def test_kalshi_prop_and_pp_leg_clv_use_last_prop_snapshot(services_mocked, mk):
    from gridiron_edge.types import PropQuote
    svc = services_mocked()
    gid = "nfl:20261031:AAA@BBB"
    svc.registry.upsert(Game(gid, "nfl", 2026, 9, KICK, "BBB", "AAA"))
    pm = replace(mk("P1", "EP", "prop", line=49.5, yes_ask=0.45, no_ask=0.60, game_id=gid),
                 player="Player 1", stat="rush_yds")
    svc.latest["kalshi"] = [pm]
    old = NOW - timedelta(hours=3)  # last paid prop pull, hours before kickoff
    svc.latest["props"] = [
        PropQuote("odds_api", "draftkings", gid, "Player 1", "rush_yds", "over", 52.5, 1.70, old),
        PropQuote("odds_api", "draftkings", gid, "Player 1", "rush_yds", "under", 52.5, 2.25, old)]
    kleg = Leg("kalshi", "x", "yes", 0.45, 0.6, gid, ticker="P1", player="Player 1", stat="rush_yds", line=49.5)
    krec = Recommendation(None, "kalshi_prop", [kleg], 0, 10, 0.05, 0.6, None, NOW - timedelta(hours=4),
                          KICK, observe_only=True)
    krec, _ = svc.lifecycle.upsert_recommendation(krec, NOW - timedelta(hours=4))
    pleg = Leg("prizepicks", "x", "over", 0.0, 0.55, gid, player="Player 1", stat="rush_yds", line=50.5,
               team="BBB", p_mkt=0.55)
    prec = Recommendation(None, "pp_entry", [pleg, replace(pleg, player="Nobody")], 0, None, 0.05, 0.3,
                          "2-power", NOW - timedelta(hours=4), KICK, observe_only=True)
    prec, _ = svc.lifecycle.upsert_recommendation(prec, NOW - timedelta(hours=4))
    svc._record_clv(NOW)
    k = svc.conn.execute("SELECT * FROM recommendations WHERE id=?", (krec.id,)).fetchone()
    assert k["clv_close"] is not None and k["clv_stale"] == 1  # labelled as a last pre-kick snapshot
    legs = svc.conn.execute("SELECT * FROM leg_clv WHERE rec_id=? ORDER BY leg_idx", (prec.id,)).fetchall()
    assert len(legs) == 1 and legs[0]["leg_idx"] == 0 and legs[0]["stale"] == 1
    assert legs[0]["clv"] == pytest.approx(legs[0]["p_close"] / 0.55 - 1)


def test_close_games_adds_observe_games_only_with_budget(services_mocked, mk):
    svc = services_mocked()
    gid, m = setup_game(svc, mk)
    svc.lifecycle.upsert_recommendation(rec_for(m, gid, observe=True), NOW - timedelta(hours=1))
    svc.odds.budget.record(400, 100, 1)  # 80% left
    assert gid in svc.close_games()
    svc.odds.budget.record(100, 400, 1)  # 20% left: no extra pulls for shadow CLV
    assert gid not in svc.close_games()
