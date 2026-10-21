from datetime import datetime, timedelta, timezone

import pytest

from gridiron_edge.bets.clv import clv
from gridiron_edge.bets.lifecycle import Lifecycle
from gridiron_edge.bets.settle import Settler, grade_kalshi_leg, grade_pp_entry
from gridiron_edge.db import Ledger
from gridiron_edge.matching import GameRegistry, TeamResolver
from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.types import Game, Leg, Recommendation

KICK = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
NOW = KICK - timedelta(hours=2)
GID = "nfl:20261025:DAL@PHI"
TICKER = "KXNFLGAME-26OCT25DALPHI-DAL"


def game(status="final", home=24, away=17, kick=KICK):
    return Game(GID, "nfl", 2026, 8, kick, "PHI", "DAL", status=status, home_pts=home, away_pts=away)


def kleg(side="yes", team="DAL", line=None, ticker=TICKER):
    return Leg("kalshi", "x", side, 0.46, 0.5, GID, ticker=ticker, team=team, line=line)


def pleg(player, line=50.5, side="over", team="DAL", game_id=GID):
    return Leg("prizepicks", "x", side, 0.0, 0.6, game_id, player=player, stat="rec_yds",
               line=line, team=team)


@pytest.fixture
def env(conn):
    ledger = Ledger(conn)
    ledger.deposit(100000)
    lc = Lifecycle(conn, ledger)
    reg = GameRegistry(conn, TeamResolver(conn))
    return conn, lc, reg, Settler(conn, lc, reg)


def place(lc, legs, kind="kalshi_game", qty=40, stake=0, structure=None):
    rec = Recommendation(None, kind, legs, stake, qty, 0.05, 0.5, structure, NOW,
                         NOW + timedelta(hours=3))
    rec, _ = lc.upsert_recommendation(rec, NOW)
    return lc.confirm(rec.id, NOW)


def no_stat(*_):
    return "pending"


def test_kalshi_result_yes_pays_qty(env, mk):
    conn, lc, reg, st = env
    reg.upsert(game())
    bet = place(lc, [kleg()])
    before = lc.ledger.balance()
    from dataclasses import replace
    m = replace(mk(TICKER, "E", "ml", team="DAL"), result="yes")
    out = st.settle_ready(NOW, lambda t: m, no_stat)
    assert out == [(bet, "won", 4000)]
    assert lc.ledger.balance() == before + 4000
    assert lc.open_bets() == []


def test_kalshi_no_side_wins_on_no(mk):
    from dataclasses import replace
    m = replace(mk(TICKER, "E", "ml", team="DAL"), result="no")
    assert grade_kalshi_leg(kleg("no"), m, None, NOW) == "won"
    assert grade_kalshi_leg(kleg("yes"), m, None, NOW) == "lost"


def test_spread_grading_from_score():
    leg = kleg(team="PHI", line=7.5)
    assert grade_kalshi_leg(leg, None, game(home=24, away=16), NOW) == "won"
    assert grade_kalshi_leg(leg, None, game(home=24, away=17), NOW) == "lost"
    assert grade_kalshi_leg(kleg("no", team="PHI", line=7.5), None, game(home=24, away=17), NOW) == "won"


def test_total_grading_from_score():
    leg = kleg(team=None, line=40.5)
    assert grade_kalshi_leg(leg, None, game(home=24, away=17), NOW) == "won"
    assert grade_kalshi_leg(leg, None, game(home=20, away=17), NOW) == "lost"


def test_nfl_tie_ml_yes_loses():
    g = game(home=20, away=20)
    assert grade_kalshi_leg(kleg(team="PHI"), None, g, NOW) == "lost"
    assert grade_kalshi_leg(kleg(team="DAL"), None, g, NOW) == "lost"


def test_live_game_pending():
    assert grade_kalshi_leg(kleg(), None, game(status="live"), NOW) == "pending"


def test_postponed_refund_after_72h(env):
    conn, lc, reg, st = env
    reg.upsert(game(status="scheduled", home=None, away=None))
    bet = place(lc, [kleg()])
    fee = round(taker_fee(40, 0.46) * 100)
    before = lc.ledger.balance()
    assert st.settle_ready(KICK + timedelta(hours=71), lambda t: None, no_stat) == []
    out = st.settle_ready(KICK + timedelta(hours=73), lambda t: None, no_stat)
    assert out == [(bet, "refund", 1840 + fee)]
    assert lc.ledger.balance() == before + 1840 + fee


def test_void_candidate_refunds_immediately():
    assert grade_kalshi_leg(kleg(), None, game(status="void_candidate"), NOW) == "refund"


def test_pp_flex_one_miss():
    legs = [pleg(f"P{i}", team=f"T{i}", game_id=f"g{i}") for i in range(5)]
    stats = {(f"P{i}", "rec_yds"): 60.0 if i < 4 else 10.0 for i in range(5)}
    assert grade_pp_entry(legs, (5, "flex"), stats) == ("won", 2.0)


def test_pp_dnp_reverts():
    legs = [pleg(f"P{i}", team=f"T{i}", game_id=f"g{i}") for i in range(3)]
    stats = {("P0", "rec_yds"): 60.0, ("P1", "rec_yds"): 70.0, ("P2", "rec_yds"): None}
    assert grade_pp_entry(legs, (3, "flex"), stats) == ("won", 3.0)


def test_pp_loss_and_tie():
    legs = [pleg(f"P{i}", team=f"T{i}", game_id=f"g{i}") for i in range(3)]
    stats = {("P0", "rec_yds"): 10.0, ("P1", "rec_yds"): 10.0, ("P2", "rec_yds"): 60.0}
    assert grade_pp_entry(legs, (3, "power"), stats) == ("lost", 0.0)
    stats = {("P0", "rec_yds"): 50.5, ("P1", "rec_yds"): 60.0, ("P2", "rec_yds"): 60.0}
    assert grade_pp_entry(legs, (3, "power"), stats) == ("won", 3.0)  # tie -> 2-power


def test_pp_single_team_after_dnp_refund():
    legs = [pleg("A", team="X", game_id="g1"), pleg("B", team="X", game_id="g2"),
            pleg("C", team="Y", game_id="g3")]
    stats = {("A", "rec_yds"): 60.0, ("B", "rec_yds"): 60.0, ("C", "rec_yds"): None}
    assert grade_pp_entry(legs, (3, "flex"), stats) == ("refund", 1.0)


def test_pp_pending_until_all_stats():
    legs = [pleg("A", team="X", game_id="g1"), pleg("B", team="Y", game_id="g2")]
    assert grade_pp_entry(legs, (2, "power"), {("A", "rec_yds"): 60.0})[0] == "pending"


def test_settle_pp_entry_pays_round_stake_mult(env):
    conn, lc, reg, st = env
    legs = [pleg(f"P{i}", team=f"T{i}", game_id=f"g{i}") for i in range(5)]
    bet = place(lc, legs, kind="pp_entry", qty=None, stake=500, structure="5-flex")
    vals = {f"P{i}": 60.0 if i < 4 else 10.0 for i in range(5)}
    assert st.settle_ready(NOW, lambda t: None, lambda g, p, s: "pending" if p == "P4" else vals[p]) == []
    out = st.settle_ready(NOW, lambda t: None, lambda g, p, s: vals[p])
    assert out == [(bet, "won", 1000)]


def test_manual_settle_and_clv(env):
    conn, lc, reg, st = env
    bet = place(lc, [kleg()])
    st.record_clv(bet, 0.55)
    fee = round(taker_fee(40, 0.46) * 100)
    assert conn.execute("SELECT clv FROM bets WHERE id=?", (bet,)).fetchone()[0] == pytest.approx(
        0.55 / ((1840 + fee) / 4000) - 1)
    st.manual_settle(bet, "lost")
    assert conn.execute("SELECT state, result, payout_cents FROM bets").fetchone()[:] == ("SETTLED", "lost", 0)
    from gridiron_edge.bets.lifecycle import LifecycleError
    with pytest.raises(LifecycleError):
        st.manual_settle(bet, "won")


def test_clv():
    assert clv(0.50, 0.55) == pytest.approx(0.10)
    assert clv(0.5, None) is None


# -- arb settlement, error isolation, clv durability ---------------------------
GID2 = "nfl:20261025:NYG@WAS"
T1, T2 = TICKER, "KXNFLGAME-26OCT25NYGWAS-NYG"


def _arb(env, mk, r1, r2):
    """Place a 2-leg arb; r1/r2 in yes/no (market result) or 'void' (game void_candidate)."""
    from dataclasses import replace
    conn, lc, reg, st = env
    reg.upsert(game(status="void_candidate" if r1 == "void" else "final"))
    reg.upsert(Game(GID2, "nfl", 2026, 8, KICK, "WAS", "NYG", status="void_candidate"))
    legs = [kleg(ticker=T1), Leg("kalshi", "x", "yes", 0.50, 0.5, GID2, ticker=T2, team="NYG")]
    bet = place(lc, legs, kind="arb", qty=10, structure="pair")
    row = conn.execute("SELECT stake_cents, fee_cents FROM bets WHERE id=?", (bet,)).fetchone()
    res = {T1: r1, T2: r2}

    def lookup(t):
        return None if res[t] == "void" else replace(mk(t, "E", "ml", team="DAL"), result=res[t])
    before = lc.ledger.balance()
    out = st.settle_ready(NOW, lookup, no_stat)
    kinds = conn.execute("SELECT kind, amount_cents FROM ledger WHERE bet_id=? AND kind IN "
                         "('payout','refund') ORDER BY id", (bet,)).fetchall()
    return bet, out, [tuple(k) for k in kinds], lc.ledger.balance() - before


def test_arb_win_and_loss(env, mk):
    bet, out, rows, delta = _arb(env, mk, "yes", "no")
    assert out == [(bet, "won", 1000)] and rows == [("payout", 1000)] and delta == 1000


def test_arb_win_and_refund(env, mk):
    refund = round(10 * 0.5 * 100) + round(taker_fee(10, 0.5, 1.0) * 100)
    bet, out, rows, delta = _arb(env, mk, "yes", "void")
    assert out == [(bet, "won", 1000 + refund)]
    assert rows == [("payout", 1000), ("refund", refund)] and delta == 1000 + refund


def test_arb_loss_and_refund_is_refund_not_won(env, mk):
    refund = round(10 * 0.5 * 100) + round(taker_fee(10, 0.5, 1.0) * 100)
    bet, out, rows, delta = _arb(env, mk, "no", "void")
    assert out == [(bet, "refund", refund)] and rows == [("refund", refund)] and delta == refund


def test_arb_all_refund(env, mk):
    bet, out, rows, delta = _arb(env, mk, "void", "void")
    conn = env[0]
    s, f = conn.execute("SELECT stake_cents, fee_cents FROM bets WHERE id=?", (bet,)).fetchone()
    assert out == [(bet, "refund", s + f)] and rows == [("refund", s + f)]


def test_bad_structure_does_not_block_later_bets(env):
    conn, lc, reg, st = env
    legs = [pleg(f"P{i}", team=f"T{i}", game_id=f"g{i}") for i in range(5)]
    bad = place(lc, legs, kind="pp_entry", qty=None, stake=500, structure="5-flex")
    conn.execute("UPDATE bets SET structure=NULL WHERE id=?", (bad,))
    conn.commit()
    reg.upsert(game())
    good = place(lc, [kleg()])
    out = st.settle_ready(NOW, lambda t: None, lambda *a: 60.0)
    assert [o[0] for o in out] == [good]
    assert conn.execute("SELECT state FROM bets WHERE id=?", (bad,)).fetchone()[0] == "PLACED"


def test_raising_lookup_isolated(env):
    conn, lc, reg, st = env
    reg.upsert(game())
    b1 = place(lc, [kleg()])
    def boom(t):
        raise ValueError("feed down")
    assert st.settle_ready(NOW, boom, no_stat) == []
    assert conn.execute("SELECT state FROM bets WHERE id=?", (b1,)).fetchone()[0] == "PLACED"


def test_record_clv_is_committed(env):
    conn, lc, reg, st = env
    bet = place(lc, [kleg()])
    st.record_clv(bet, 0.55)
    assert conn.in_transaction is False


def test_middle_settles_like_a_pair(env):
    conn, lc, reg, st = env
    reg.upsert(game(home=27, away=24))  # PHI by 3: both a YES PHI >2.5 and a NO PHI >3.5 pay
    lo = Leg("kalshi", "x", "yes", 0.52, 0.5, GID, ticker="SP-PHI3", team="PHI", line=2.5)
    hi = Leg("kalshi", "x", "no", 0.50, 0.5, GID, ticker="SP-PHI4", team="PHI", line=3.5)
    bet_id = place(lc, [lo, hi], kind="middle", qty=10)
    done = st.settle_ready(KICK + timedelta(hours=4), lambda t: None, no_stat)
    assert done == [(bet_id, "won", 2000)]


def test_pp_grading_uses_configured_table():
    legs = [pleg(f"P{i}", team=f"T{i}", game_id=f"g{i}") for i in range(5)]
    stats = {(f"P{i}", "rec_yds"): 60.0 if i < 4 else 10.0 for i in range(5)}
    arena = {(5, "flex"): {5: 10}, (4, "flex"): {4: 6}}
    assert grade_pp_entry(legs, (5, "flex"), stats, arena) == ("lost", 0.0)


def test_settler_takes_payout_table(env):
    conn, lc, reg, _ = env
    st = Settler(conn, lc, reg, payouts={(2, "power"): {2: 3.5}})
    assert st.payouts[(2, "power")] == {2: 3.5}


def test_middle_with_one_leg_paying_is_lost(env):
    conn, lc, reg, st = env
    reg.upsert(game(home=31, away=24))  # PHI by 7: YES PHI >2.5 pays, NO PHI >3.5 loses
    lo = Leg("kalshi", "x", "yes", 0.52, 0.5, GID, ticker="SP-PHI3", team="PHI", line=2.5)
    hi = Leg("kalshi", "x", "no", 0.50, 0.5, GID, ticker="SP-PHI4", team="PHI", line=3.5)
    bet_id = place(lc, [lo, hi], kind="middle", qty=10, stake=1020)
    done = st.settle_ready(KICK + timedelta(hours=4), lambda t: None, no_stat)
    assert done == [(bet_id, "lost", 1000)]  # $1 a pair back on $1.02 a pair: a loss, payout kept


def test_pp_exact_hit_on_whole_number_line_is_a_push_not_an_under():
    # PrizePicks treats a stat landing exactly on the line as a push: the pick is dropped (refund),
    # never graded as a win for LESS
    legs = [pleg(f"P{i}", line=50.0, side="under", team=f"T{i}", game_id=f"g{i}") for i in range(3)]
    stats = {("P0", "rec_yds"): 50, ("P1", "rec_yds"): 40.0, ("P2", "rec_yds"): 30.0}
    assert grade_pp_entry(legs, (3, "power"), stats) == ("won", 3.0)  # 2 hits on a 2-pick power after the push
    stats = {("P0", "rec_yds"): 50, ("P1", "rec_yds"): 50.0, ("P2", "rec_yds"): 30.0}
    assert grade_pp_entry(legs, (3, "power"), stats) == ("refund", 1.0)  # one pick left: the entry is refunded
