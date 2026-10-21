from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gridiron_edge.bets.lifecycle import Lifecycle, LifecycleError
from gridiron_edge.db import InsufficientFunds, Ledger
from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.types import Leg, Recommendation

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)


def kalshi_rec(price=0.46, qty=40, ticker="KXNFLGAME-26OCT25DALPHI-DAL", observe=False):
    leg = Leg("kalshi", "DAL ML", "yes", price, 0.52, "nfl:20261025:DAL@PHI", ticker=ticker)
    return Recommendation(None, "kalshi_game", [leg], 0, qty, 0.05, 0.52, None, NOW,
                          NOW + timedelta(hours=3), observe_only=observe)


@pytest.fixture
def lc(conn):
    ledger = Ledger(conn)
    ledger.deposit(10000)
    return Lifecycle(conn, ledger)


def test_dedupe_same_key(lc, conn):
    r1, new1 = lc.upsert_recommendation(kalshi_rec(), NOW)
    r2, new2 = lc.upsert_recommendation(kalshi_rec(), NOW)
    assert (new1, new2) == (True, False)
    assert r1.id == r2.id
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1


def test_price_move_expires_old(lc):
    old, _ = lc.upsert_recommendation(kalshi_rec(price=0.46), NOW)
    new, is_new = lc.upsert_recommendation(kalshi_rec(price=0.48), NOW)
    assert is_new
    assert lc.get_rec(old.id).state == "EXPIRED"
    assert lc.get_rec(new.id).state == "RECOMMENDED"


def test_confirm_texted_id_after_price_tick_resolves_to_live_rec(lc):
    old, _ = lc.upsert_recommendation(kalshi_rec(price=0.46), NOW)
    lc.mark_pending(old.id)  # texted
    new, _ = lc.upsert_recommendation(kalshi_rec(price=0.47), NOW)
    lc.confirm(old.id, NOW, price=0.46)
    assert lc.get_rec(new.id).state == "PLACED"
    assert lc.get_rec(old.id).state == "EXPIRED"
    assert lc.open_bets()[0]["rec_id"] == new.id and lc.open_bets()[0]["price"] == 0.46


def test_confirm_expired_without_successor_raises(lc):
    old, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    lc.expire_due(old.expires_at)
    with pytest.raises(LifecycleError, match="not pending"):
        lc.confirm(old.id, NOW)


def test_confirm_debits_with_fee(lc):
    rec, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    lc.mark_pending(rec.id)
    bet_id = lc.confirm(rec.id, NOW)
    fee = round(taker_fee(40, 0.46) * 100)
    assert lc.ledger.balance() == 10000 - 1840 - fee
    assert lc.get_rec(rec.id).state == "PLACED"
    b = lc.open_bets()[0]
    assert b["id"] == bet_id and b["fee_cents"] == fee and b["state"] == "PLACED"
    assert lc.open_exposure_cents() == 1840


def test_confirm_override_price_qty(lc):
    rec, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    lc.confirm(rec.id, NOW, price=0.47, qty=30)
    b = lc.open_bets()[0]
    assert b["price"] == 0.47 and b["qty"] == 30
    assert b["stake_cents"] == 1410


def test_confirm_expired_raises(lc):
    rec, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    with pytest.raises(LifecycleError, match="expired"):
        lc.confirm(rec.id, rec.expires_at)
    assert lc.ledger.balance() == 10000


def test_confirm_unknown_id(lc):
    with pytest.raises(LifecycleError, match="unknown id"):
        lc.confirm(999, NOW)


def test_confirm_insufficient_funds_atomic(conn):
    ledger = Ledger(conn)
    ledger.deposit(500)
    lc = Lifecycle(conn, ledger)
    rec, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    lc.mark_pending(rec.id)
    with pytest.raises(InsufficientFunds):
        lc.confirm(rec.id, NOW)
    assert conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
    assert lc.get_rec(rec.id).state == "PENDING"
    assert ledger.balance() == 500


def test_observe_only_logged_not_confirmable(lc):
    rec, _ = lc.upsert_recommendation(kalshi_rec(observe=True), NOW)
    assert lc.get_rec(rec.id).state == "LOGGED"
    with pytest.raises(LifecycleError, match="not pending"):
        lc.confirm(rec.id, NOW)


def test_expire_due(lc):
    a, _ = lc.upsert_recommendation(kalshi_rec(ticker="A"), NOW)
    b, _ = lc.upsert_recommendation(kalshi_rec(ticker="B"), NOW)
    c, _ = lc.upsert_recommendation(replace(kalshi_rec(ticker="C"), expires_at=NOW + timedelta(days=2)), NOW)
    lc.mark_pending(b.id)
    done = lc.expire_due(NOW + timedelta(hours=4))
    assert sorted(done) == sorted([a.id, b.id])
    assert lc.get_rec(c.id).state == "RECOMMENDED"
    assert lc.pending() == [lc.get_rec(c.id)]


def test_pp_confirm_and_daily_total(lc):
    leg = Leg("prizepicks", "x over 50.5", "over", 0.0, 0.6, "g", player="X", stat="rec_yds", line=50.5)
    rec = Recommendation(None, "pp_entry", [leg], 500, None, 0.1, 0.3, "2-3", NOW,
                         NOW + timedelta(hours=3))
    rec, _ = lc.upsert_recommendation(rec, NOW)
    lc.confirm(rec.id, NOW)
    assert lc.ledger.balance() == 9500
    assert lc.pp_staked_today_cents(NOW, timezone.utc) == 500
    assert lc.pp_staked_today_cents(NOW + timedelta(days=1), timezone.utc) == 0


def test_arb_confirm_sums_legs(lc):
    legs = [Leg("kalshi", "a", "yes", 0.40, 0.5, "g", ticker="T1"),
            Leg("kalshi", "b", "no", 0.55, 0.5, "g", ticker="T2")]
    rec = Recommendation(None, "arb", legs, 0, 10, 0.05, 1.0, "pair", NOW, NOW + timedelta(hours=1))
    rec, _ = lc.upsert_recommendation(rec, NOW)
    lc.confirm(rec.id, NOW)
    fees = round(taker_fee(10, 0.40) * 100) + round(taker_fee(10, 0.55) * 100)
    assert lc.ledger.balance() == 10000 - 950 - fees


# -- quant upgrade: market fee multiplier, exposure queries -------------------------------------

def test_confirm_uses_market_fee_multiplier(lc):
    rec = kalshi_rec(price=0.46, qty=40)
    rec = replace(rec, legs=[replace(rec.legs[0], fee_multiplier=0.5)])
    saved, _ = lc.upsert_recommendation(rec, NOW)
    bet_id = lc.confirm(saved.id, NOW)
    row = lc.conn.execute("SELECT fee_cents FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row["fee_cents"] == round(taker_fee(40, 0.46, 0.5) * 100)


def test_open_positions_and_placed_today(lc):
    from zoneinfo import ZoneInfo
    a, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    lc.confirm(a.id, NOW)
    b, _ = lc.upsert_recommendation(kalshi_rec(ticker="OTHER"), NOW)
    lc.confirm(b.id, NOW - timedelta(days=2))
    pos = lc.open_positions()
    assert [(k, s) for k, s, _ in pos] == [("kalshi_game", round(40 * 0.46 * 100))] * 2
    assert pos[0][2][0].ticker == "KXNFLGAME-26OCT25DALPHI-DAL"
    pt = ZoneInfo("America/Los_Angeles")
    assert lc.placed_today_cents(NOW, pt) == round(40 * 0.46 * 100)


def test_placed_today_filters_by_day_and_skips_cancelled(lc):
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    a, _ = lc.upsert_recommendation(kalshi_rec(), NOW)
    lc.confirm(a.id, NOW)
    b, _ = lc.upsert_recommendation(kalshi_rec(ticker="OTHER"), NOW)
    other = lc.confirm(b.id, NOW)
    stake = round(40 * 0.46 * 100)
    assert lc.placed_today_cents(NOW, pt) == 2 * stake
    # a bet stamped in another timezone on the same PT day still counts once
    lc.conn.execute("UPDATE bets SET placed_at=? WHERE id=?", (NOW.astimezone(pt).isoformat(), other))
    assert lc.placed_today_cents(NOW, pt) == 2 * stake
    lc.conn.execute("UPDATE bets SET state='CANCELLED' WHERE id=?", (other,))
    assert lc.placed_today_cents(NOW, pt) == stake
    assert lc.placed_today_cents(NOW + timedelta(days=1), pt) == 0
