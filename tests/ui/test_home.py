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
    assert cells[3] == "47 cents" and cells[4] == "$18.80"
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
    assert v.game == "Cowboys 10, Eagles 14 - 2nd quarter, 3:00 left"
    # 40 x 60c = $24.00 less a $0.68 fee to sell, against $19.50 paid
    assert v.pnl_cents == 2400 - 68 - 1950
    assert v.worth == "sell now for $23.32 \u00b7 paid $19.50 \u00b7 +$3.82" and v.paid_text == ""
    assert v.pays == "pays $40.00 if it wins" and v.max_payout_cents == 4000
    assert v.chances == "our chance 55% when placed \u00b7 market says 53% now"  # mid of 60 bid / 47 ask
    assert v.expected_cents == 2200  # 40 x 55%
    assert v.close == "price 2.1% better than closing"
    assert home.close_text(-0.008) == "price 0.8% worse than closing"


def test_open_bet_pp_legs_and_scheduled():
    legs = [asdict(leg(venue="prizepicks", side="over", player=f"P{i}", stat="rush_yds", line=50.5,
                       ticker=None, team=None)) for i in range(3)]
    row = bet_row(kind="pp_entry", legs_json=json.dumps(legs), structure="3-power", qty=None, fee_cents=0,
                  stake_cents=1000)
    stats = {"P0": 60.0, "P1": 20.0}
    v = home.open_bet_view(row, {GID: GAME}, {}, NOW, stat_lookup=lambda g, p, s: stats.get(p, "pending"))
    assert v.worth == "1 hit, 1 missed, 1 pending" and v.game == "kickoff 1:20 PM PT" and v.close == "not yet"


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
    assert "watch-only" not in home.empty_reason(NOW, **{**base, "gates": {("nfl", "ml"): True}})
    gated = {("nfl", m): True for m in ("ml", "spread", "total")} | {("cfb", "ml"): False}
    assert "watch-only" in home.empty_reason(NOW, **{**base, "gates": gated})
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


# -- info line, detail lines, WATCHING, open-bet values ---------------------------------------------
def test_age_and_info_segments():
    assert home.age_text(None, NOW, "Kalshi prices") == "no Kalshi prices yet"
    assert home.age_text(NOW - timedelta(seconds=20), NOW, "Kalshi prices") == "Kalshi prices under 1 min old"
    assert home.age_text(NOW - timedelta(minutes=12), NOW, "sportsbook lines") == "sportsbook lines 12 min old"
    assert home.age_text(NOW - timedelta(minutes=150), NOW, "player lines") == "player lines 2 hr old"
    assert home.age_text(NOW - timedelta(days=1, hours=1), NOW, "player lines") == "player lines 1 day old"
    assert home.record_text(0, 0, 0) == "Today: no bets settled yet"
    assert home.record_text(2, 1, 1) == "Today: 2 won, 1 lost, 1 refunded"
    segs = home.info_segments(NOW, record=(2, 1, 0), day_left=1240, day_cap=3580,
                              next_check=NOW + timedelta(minutes=12), kalshi_at=NOW - timedelta(minutes=1),
                              books_at=NOW - timedelta(minutes=12), props_at=NOW - timedelta(hours=2))
    assert segs == ["Today: 2 won, 1 lost", "$12.40 of today's $35.80 limit left", "next check 12:42 PM PT",
                    "Kalshi prices 1 min old", "sportsbook lines 12 min old", "player lines 2 hr old"]
    assert home.info_segments(NOW, record=(0, 0, 0), day_left=None, day_cap=None, next_check=NOW,
                              kalshi_at=None, books_at=None, props_at=None)[1] == "checking prices now"


def test_pack_breaks_only_between_segments():
    segs = ["aaaa", "bbbb", "cccc"]
    assert home.pack(segs, 200) == ["aaaa · bbbb · cccc"]
    assert home.pack(segs, 11) == ["aaaa · bbbb", "cccc"]
    assert home.pack(["a very long segment"], 5) == ["a very long segment"]  # never split a segment


def test_max_price_keeps_the_minimum_edge():
    top = home.max_price(leg(p_fair=0.55), 40, 0.025)
    from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev
    assert kalshi_ev(0.55, effective_price(top, 40)) >= 0.025
    assert kalshi_ev(0.55, effective_price(top + 0.01, 40)) < 0.025
    assert home.max_price(leg(p_fair=0.01), 40, 0.025) is None


def test_short_player_and_source():
    assert home.short_player("Jalen Hurts") == "Hurts"
    assert home.short_player("Kenneth Walker III") == "Walker"
    assert home.short_player("Amon-Ra St. Brown") == "St. Brown"
    pin = {"fair_source": "pinnacle", "books": ["pinnacle", "draftkings", "fanduel"], "model_weight": 0.0}
    assert home.source_short(rec(1, why=pin)) == "from Pinnacle and 2 other sportsbooks"
    blended = {**pin, "model_weight": 0.3, "p_model": 0.55}
    assert home.source_short(rec(1, why=blended)) == "from Pinnacle and 2 other sportsbooks plus our team ratings"
    assert home.source_short(rec(1, why={"fair_source": "pinnacle", "books": ["pinnacle"]})) == "from Pinnacle"
    assert home.source_short(rec(1, why={"fair_source": "exchanges"})) == "from betting exchanges"
    proj = {"kalshi_mid": 0.4, "weight": 0.3, "p_ours": 0.5, "book_line": None}
    assert home.source_short(rec(1, kind="kalshi_prop", why=proj)) == "from our player projection and Kalshi traders"
    assert home.source_short(rec(1, kind="kalshi_prop", why={**proj, "book_line": 1.5})) == \
        "from sportsbook player lines and Kalshi traders"
    assert home.source_short(rec(1)) == "from sportsbook lines"


def test_place_now_detail_kalshi_single():
    r = rec(9, legs=[leg(p_fair=0.52, p_mkt=0.51)], qty=40,
            why={"fair_source": "pinnacle", "books": ["pinnacle", "draftkings", "fanduel"]})
    text = home.place_now_detail(r, {}, {GID: GAME}, 0.025)
    top = round(home.max_price(r.legs[0], 40, 0.025) * 100)
    assert text == (f"our chance 52% · price says 47% · sportsbooks say 51% · 40 contracts, pay up to "
                    f"{top} cents · pays $40.00 if it wins · from Pinnacle and 2 other sportsbooks")


def test_place_now_detail_locked_profit_shows_worst_and_best():
    ml = dict(market_type="ml", line=None)
    markets = {"KC": market(ticker="KC", team="PHI", **ml), "MIA": market(ticker="MIA", team="DAL", **ml)}
    arb = rec(12, kind="arb", qty=20, tie_risk=True,
              legs=[leg(ticker="KC", line=None, price=0.60, worst_price=0.60),
                    leg(ticker="MIA", team="DAL", line=None, price=0.36, worst_price=0.37)])
    text = home.place_now_detail(arb, markets, {GID: GAME}, 0.025)
    assert "20 contracts of each side, pay up to 60 cents and 37 cents" in text
    assert "pays $20.00 whichever team wins ($0.00 on a tie)" in text
    assert text.endswith("from the prices alone (no prediction needed)")
    spread = dict(market_type="spread")
    ladder = {"A": market(ticker="A", line=2.5, **spread), "B": market(ticker="B", line=6.5, **spread)}
    mid = rec(13, kind="middle", qty=20, p_win=0.16, legs=[leg(ticker="A", line=2.5), leg(ticker="B", side="no",
                                                                                        line=6.5)])
    text = home.place_now_detail(mid, ladder, {GID: GAME}, 0.025)
    assert "both sides pay about 16% of the time" in text and "pays $20.00 at worst, $40.00 at best" in text


def test_place_now_detail_prizepicks_needed_chance():
    legs = [leg(venue="prizepicks", side="over", player=n, stat="pass_tds", line=1.5, ticker=None, p_fair=p,
                p_mkt=p) for n, p in (("Jalen Hurts", 0.61), ("Patrick Mahomes", 0.62), ("Jared Goff", 0.6))]
    pp = rec(11, kind="pp_entry", legs=legs, structure="3-power", stake_cents=1000, qty=None)
    text = home.place_now_detail(pp, {}, {GID: GAME}, 0.025)
    need = home.pp_needed("3-power")
    assert text == ("our chance per pick: Hurts 61%, Mahomes 62%, Goff 60% · breaks even at about "
                    f"{need:.0%} per pick · pays up to $60.00 if all 3 hit · from sportsbook player lines")


def test_watching_filters_dedupes_and_ranks():
    games = {GID: GAME}
    a_old = rec(1, observe_only=True, state="LOGGED", ev=0.05)
    a_new = rec(4, observe_only=True, state="LOGGED", ev=0.04)  # same idea, newer price
    b = rec(2, observe_only=True, state="LOGGED", ev=0.09, legs=[leg(side="no")])
    texted = rec(3, ev=0.2)  # a real rec: in PLACE NOW, not here
    stale = rec(5, observe_only=True, state="LOGGED", expires_at=NOW, legs=[leg(line=6.5)])
    out = home.watching([a_old, a_new, b, texted, stale], NOW, games)
    assert [r.id for r in out] == [2, 4]
    started = {GID: replace(GAME, status="live")}
    assert home.watching([b], NOW, started) == []
    assert home.watch_summary(2, 2) is None
    assert home.watch_summary(8, 11) == "3 more ideas on the watch list not shown."


def test_watch_reasons():
    games = {GID: GAME, "cfb:1:A@B": replace(GAME, game_id="cfb:1:A@B", league="cfb")}
    r = rec(1, observe_only=True)
    assert home.watch_reason(r, games, {}, {("nfl", "spread"): True}) == "paused after poor recent results"
    assert home.watch_reason(r, games, {("nfl", "spread"): True}, {}) == \
        "our model is still being tested for this kind of bet"
    cfb = rec(2, observe_only=True, legs=[leg(game_id="cfb:1:A@B")])
    assert home.watch_reason(cfb, games, {("cfb", "spread"): True}, {}) == "our college model is still being tested"
    prop = rec(3, kind="kalshi_prop", observe_only=True, legs=[leg(player="Jordan Love", stat="pass_tds", line=1.5)],
               why={"kalshi_mid": 0.4, "book_line": None})
    assert home.watch_reason(prop, games, {}, {}) == "our player projection is still being tested"
    mid = rec(4, kind="middle", observe_only=True, legs=[leg(), leg(side="no", line=6.5)])
    assert home.watch_reason(mid, games, {}, {}, middles_live=False) == \
        "pairs that are not risk-free are watch-only for now"
    head, detail = home.watch_cells(rec(9, ev=0.07), NOW, games, "our model is still being tested")
    assert head == "#9 Kalshi  Eagles win by 4 or more points vs Cowboys"
    assert detail == ("    47 cents · our chance 55% · about 7 cents profit per $1 bet · kickoff 1:20 PM PT"
                      " · not texted: our model is still being tested")


def test_open_bet_without_a_buyer_or_a_price():
    v = home.open_bet_view(bet_row(), {GID: GAME}, {"KXNFLSPREAD-T": market(yes_bid=0.0)}, NOW)
    assert v.worth == "no buyer right now" and v.paid_text == "paid $19.50" and v.pnl_cents is None
    v = home.open_bet_view(bet_row(), {GID: GAME}, {}, NOW)
    assert v.worth == "no current Kalshi price" and v.chances == "our chance 55% when placed"


def test_open_bet_pp_pick_progress_and_summary():
    legs = [asdict(leg(venue="prizepicks", side="over", player=n, stat="pass_tds", line=1.5, ticker=None,
                       team=None, p_fair=0.6)) for n in ("Jalen Hurts", "Jared Goff", "Dak Prescott")]
    row = bet_row(id=8, kind="pp_entry", legs_json=json.dumps(legs), structure="3-power", qty=None, fee_cents=0,
                  stake_cents=1000)
    stats = {"Jalen Hurts": 1.0, "Jared Goff": 3.0}
    v = home.open_bet_view(row, {GID: GAME}, {}, NOW, stat_lookup=lambda g, p, s: stats.get(p, "pending"))
    assert v.picks == ("Hurts 1 of 1.5 passing touchdowns (needed more; missed) · Goff 3 of 1.5 passing "
                       "touchdowns (needed more; hit) · Prescott: no stats yet")
    assert v.pays == "pays up to $60.00 if all 3 hit" and v.max_payout_cents == 6000
    assert v.expected_cents == round(1000 * 6 * 0.6 ** 3)
    quiet = home.open_bet_view(row, {GID: GAME}, {}, NOW)
    assert quiet.picks == "" and quiet.worth == "3 pending"
    k = home.open_bet_view(bet_row(), {GID: GAME}, {}, NOW)
    assert home.open_summary([]) is None
    assert home.open_summary([k, v]) == [
        "$28.80 in 2 open bets ($29.50 with fees)", "up to $100.00 back if all win",
        f"about {home.d.money(k.expected_cents + v.expected_cents)} expected back "
        f"(+{home.d.money(k.expected_cents + v.expected_cents - 2950)}) at our chances when placed"]


# -- review fixes ----------------------------------------------------------------------------------
def test_no_side_game_bet_sportsbooks_number_matches_why_panel():
    from gridiron_edge import describe as d
    why = {"fair_source": "pinnacle", "books": ["pinnacle"], "p_market": 0.42, "p_model": 0.40,
           "model_weight": 0.3, "method": "h2h", "tie_prob": 0.0}
    r = rec(16, legs=[leg(side="no", price=0.52, p_fair=0.60, p_mkt=0.58)], why=why)
    detail = home.place_now_detail(r, {}, {GID: GAME}, 0.025)
    assert "sportsbooks say 58%" in detail
    why_text = "\n".join(d.why_lines(r, NOW))
    assert "Sportsbooks say 58%; our team ratings say 60% and count for 30%" in why_text
    no_mkt = rec(17, legs=[leg(side="no", price=0.52, p_fair=0.60)], why=why)  # older rec: no p_mkt on the leg
    assert "Sportsbooks say 58%; our team ratings say 60%" in "\n".join(d.why_lines(no_mkt, NOW))
    assert d.model_share_text(why) == ("Sportsbooks say 42%; our team ratings say 40% and count for 30% "
                                       "of our number")


def test_prizepicks_detail_says_break_even():
    legs = [leg(venue="prizepicks", side="over", player=n, stat="pass_tds", line=1.5, ticker=None, p_fair=0.6,
                p_mkt=0.6) for n in ("Jalen Hurts", "Patrick Mahomes", "Jared Goff")]
    pp = rec(11, kind="pp_entry", legs=legs, structure="3-power", stake_cents=1000, qty=None)
    text = home.place_now_detail(pp, {}, {GID: GAME}, 0.025)
    assert f"breaks even at about {home.pp_needed('3-power'):.0%} per pick" in text and "needs" not in text


def test_pair_without_worst_price_says_at_not_up_to():
    spread = dict(market_type="spread")
    ladder = {"A": market(ticker="A", line=2.5, **spread), "B": market(ticker="B", line=6.5, **spread)}
    mid = rec(13, kind="middle", qty=20, p_win=0.16,
              legs=[leg(ticker="A", line=2.5, price=0.47), leg(ticker="B", side="no", line=6.5, price=0.50)])
    text = home.place_now_detail(mid, ladder, {GID: GAME}, 0.025)
    assert "20 contracts of each side at 47 cents and 50 cents" in text and "up to" not in text.split(" · ")[1]
    capped = rec(14, kind="middle", qty=20, p_win=0.16,
                 legs=[leg(ticker="A", line=2.5, price=0.47, worst_price=0.48),
                       leg(ticker="B", side="no", line=6.5, price=0.50, worst_price=0.51)])
    assert "20 contracts of each side, pay up to 48 cents and 51 cents" in \
        home.place_now_detail(capped, ladder, {GID: GAME}, 0.025)


def test_open_bet_game_text_is_lowercase_and_zero_contracts_reason():
    assert home.game_text(GAME, NOW) == "kickoff 1:20 PM PT"
    v = home.open_bet_view(bet_row(qty=0), {GID: GAME}, {"KXNFLSPREAD-T": market()}, NOW)
    assert v.worth == "no contracts to sell"
