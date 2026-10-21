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
                          NOW - timedelta(hours=1), KICK, observe_only=observe)


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
