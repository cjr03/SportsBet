import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from gridiron_edge.types import Game, KalshiMarket, Leg, Recommendation
from gridiron_edge.ui import home

NOW = datetime(2026, 10, 25, 19, 30, tzinfo=timezone.utc)  # 12:30 PM PT
GID = "nfl:20261025:DAL@PHI"
GAME = Game(GID, "nfl", 2026, 7, NOW + timedelta(minutes=50), "PHI", "DAL")


def leg(**kw):
    base = dict(venue="kalshi", description="x", side="yes", price=0.47, p_fair=0.55, game_id=GID,
                ticker="KXNFLSPREAD-T", team="PHI", line=3.5)
    base.update(kw)
    return Leg(**base)


def rec(i, **kw):
    base = dict(id=i, kind="kalshi_game", legs=[leg()], stake_cents=1880, qty=40, ev=0.1, p_win=0.55,
                structure=None, snapshot_ts=NOW, expires_at=GAME.kickoff)
    base.update(kw)
    return Recommendation(**base)


def market(**kw):
    base = dict(ticker="KXNFLSPREAD-T", event_ticker="E", series="S", market_type="spread", game_id=GID,
                team="PHI", line=3.5, player=None, stat=None, yes_bid=0.46, yes_ask=0.47, no_bid=0.53,
                no_ask=0.54, yes_ask_size=100, no_ask_size=100, status="active", result="",
                fee_multiplier=1.0, fetched_at=NOW)
    base.update(kw)
    return KalshiMarket(**base)


def test_actionable_filters_and_sorts():
    games = {GID: GAME}
    small = rec(1, qty=10, stake_cents=470)
    big = rec(2)
    observe = rec(3, observe_only=True)
    skipped = rec(4, state="SKIPPED")
    expired = rec(5, expires_at=NOW - timedelta(seconds=1))
    out = home.actionable([small, big, observe, skipped, expired], NOW, games, {}, 0.025)
    assert [r.id for r in out] == [2, 1]  # bigger expected profit first


def test_actionable_drops_started_closed_and_repriced():
    started = {GID: replace(GAME, kickoff=NOW - timedelta(minutes=1))}
    assert home.actionable([rec(1)], NOW, started, {}, 0.025) == []
    live = {GID: replace(GAME, status="live")}
    assert home.actionable([rec(1)], NOW, live, {}, 0.025) == []
    closed = {"KXNFLSPREAD-T": market(status="closed")}
    assert home.actionable([rec(1)], NOW, {GID: GAME}, closed, 0.025) == []
    moved = {"KXNFLSPREAD-T": market(yes_ask=0.55)}  # edge gone at the new price
    assert home.actionable([rec(1)], NOW, {GID: GAME}, moved, 0.025) == []
    small_move = {"KXNFLSPREAD-T": market(yes_ask=0.48)}
    assert [r.id for r in home.actionable([rec(1)], NOW, {GID: GAME}, small_move, 0.025)] == [1]


def test_place_now_cells_plain():
    cells = home.place_now_cells(rec(41), NOW, {GID: GAME})
    assert cells[0] == "41" and cells[1] == "Kalshi"
    assert cells[2] == "Eagles win by 4 or more points vs Cowboys"
    assert cells[3] == "47c" and cells[4] == "$18.80"
    assert cells[5] == "$2.50" and cells[6] == "50 min left" and cells[7] == "1:20 PM"
    arb = rec(42, kind="arb", legs=[leg(line=None), leg(team="DAL", line=None, price=0.48)], qty=20)
    assert home.place_now_cells(arb, NOW, {GID: GAME})[1] == "Kalshi - Locked profit"


def bet_row(**kw):
    base = dict(id=7, rec_id=41, kind="kalshi_game", legs_json=json.dumps([asdict(leg())]), price=0.47,
                qty=40, stake_cents=1880, fee_cents=70, structure=None, placed_at=NOW.isoformat(),
                state="PLACED", result=None, payout_cents=None, settled_at=None, clv=None)
    base.update(kw)
    return base


def test_open_bet_kalshi_live_with_value():
    g = replace(GAME, status="live", home_pts=14, away_pts=10, period="Q2", clock="3:00")
    v = home.open_bet_view(bet_row(clv=0.021), {GID: g}, {"KXNFLSPREAD-T": market(yes_bid=0.60)}, NOW)
    assert v.text == "Eagles win by 4 or more points vs Cowboys" and v.stake == "$19.50"
    assert v.game == "Cowboys 10, Eagles 14 - Q2 3:00"
    # 40 x 60c = $24.00 less a $0.68 fee to sell, against $19.50 paid
    assert v.pnl_cents == 2400 - 68 - 1950 and v.worth == "$23.32 (+$3.82)"
    assert v.close == "+2.1%"


def test_open_bet_pp_legs_and_scheduled():
    legs = [asdict(leg(venue="prizepicks", side="over", player=f"P{i}", stat="rush_yds", line=50.5,
                       ticker=None, team=None)) for i in range(3)]
    row = bet_row(kind="pp_entry", legs_json=json.dumps(legs), structure="3-power", qty=None, fee_cents=0,
                  stake_cents=1000)
    stats = {"P0": 60.0, "P1": 20.0}
    v = home.open_bet_view(row, {GID: GAME}, {}, NOW, stat_lookup=lambda g, p, s: stats.get(p, "pending"))
    assert v.worth == "1 hit, 1 missed, 1 pending" and v.game == "Kickoff 1:20 PM" and v.close == "not yet"


def test_open_bet_pp_exact_line_is_a_push():
    legs = [asdict(leg(venue="prizepicks", side="under", player=f"P{i}", stat="rush_yds", line=50.0,
                       ticker=None, team=None)) for i in range(2)]
    row = bet_row(kind="pp_entry", legs_json=json.dumps(legs), structure="2-power", qty=None, fee_cents=0)
    stats = {"P0": 50.0, "P1": 20.0}
    v = home.open_bet_view(row, {GID: GAME}, {}, NOW, stat_lookup=lambda g, p, s: stats[p])
    assert v.worth == "1 hit, 1 pushed (exactly on the line)"


def test_open_bet_worth_now_uses_the_market_fee_multiplier():
    row = bet_row(legs_json=json.dumps([asdict(leg(fee_multiplier=0.5))]))
    v = home.open_bet_view(row, {GID: GAME}, {"KXNFLSPREAD-T": market(yes_bid=0.60)}, NOW)
    from gridiron_edge.strategy.kalshi_pricing import taker_fee
    assert v.pnl_cents == 2400 - round(taker_fee(40, 0.60, 0.5) * 100) - 1950


def test_empty_reasons():
    nxt = NOW + timedelta(minutes=3)
    base = dict(odds_key=True, games=[GAME], markets=[market()], gates={("nfl", "ml"): False}, next_check=nxt)
    assert home.empty_reason(NOW, **base) == "No good bets right now. Next check in 3 min."
    assert "isn't set" in home.empty_reason(NOW, **{**base, "odds_key": False})
    assert home.empty_reason(NOW, **{**base, "markets": []}) == \
        "Kalshi lines for the 1:20 PM games aren't open yet. Next check in 3 min."
    assert "tracking-only" not in home.empty_reason(NOW, **{**base, "gates": {("nfl", "ml"): True}})
    gated = {("nfl", m): True for m in ("ml", "spread", "total")} | {("cfb", "ml"): False}
    assert "tracking-only" in home.empty_reason(NOW, **{**base, "gates": gated})
    assert home.empty_reason(NOW, **{**base, "games": []}).startswith("No games on the schedule")


# -- PLACE NOW respects the sizing caps and later pauses ---------------------------------------------
class _Snap:
    def __init__(self, **kw):
        self.games = {GID: GAME}
        self.open_game_exposure = kw.get("game", {})
        self.open_window_exposure = {}
        self.open_dir_exposure = kw.get("dirs", {})
        self.placed_today_cents = kw.get("today", 0)
        self.bankroll_cents = 100_000
        self.open_exposure_cents = 0


def _budget(**kw):
    from gridiron_edge.config import Settings
    from gridiron_edge.pipeline import Budget
    snap = _Snap(**kw)
    return Budget(snap.bankroll_cents, snap.bankroll_cents, Settings(cap_game=0.04, cap_day=0.2,
                                                                     cap_same_dir=0.03), snap)


def test_within_limits_drops_recs_the_game_cap_no_longer_allows():
    a, b = rec(1, stake_cents=2500), rec(2, stake_cents=2000, legs=[leg(side="no")])
    assert [r.id for r in home.within_limits([a, b], {GID: GAME}, _budget())] == [1]  # 4% of $1,000 = $40
    placed = _budget(game={GID: 3000})  # $30 already placed on this game
    assert home.within_limits([a], {GID: GAME}, placed) == []
    assert home.within_limits([a], {GID: GAME}, _budget(today=19_000)) == []  # daily cap nearly used


def test_within_limits_same_direction_cap_and_hedged_pairs():
    yes1, yes2 = rec(1, stake_cents=2000), rec(2, stake_cents=2000)
    assert [r.id for r in home.within_limits([yes1, yes2], {GID: GAME}, _budget())] == [1]
    arb = rec(3, kind="arb", stake_cents=9000, legs=[leg(), leg(side="no")])
    assert home.within_limits([arb], {GID: GAME}, _budget(game={GID: 4000})) == [arb]


def test_within_limits_drops_markets_paused_since_the_rec():
    paused = {("nfl", "spread"): True}
    assert home.within_limits([rec(1)], {GID: GAME}, _budget(), paused) == []
    ml = rec(2, legs=[leg(line=None, ticker="KXNFLGAME-T")])
    assert home.within_limits([ml], {GID: GAME}, _budget(), paused) == [ml]


def test_middle_row_is_labelled_not_risk_free():
    mid = rec(1, kind="middle", legs=[leg(line=2.5), leg(side="no", line=6.5)], p_win=0.2, stake_cents=0)
    assert home.place_now_cells(mid, NOW, {GID: GAME})[1] == "Kalshi - Not risk-free"
