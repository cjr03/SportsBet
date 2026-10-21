from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gridiron_edge.models import Prediction
from gridiron_edge.pipeline import (
    Snapshot,
    arb_candidates,
    kalshi_game_candidates,
    kalshi_prop_candidates,
    pp_entry_candidates,
    scan,
)
from gridiron_edge.strategy.distributions import (
    CFB_SIGMA,
    NFL_ML_SIGMA,
    NFL_SPREAD_SIGMA,
    NFL_TOTAL_SIGMA,
    NormalModel,
)
from gridiron_edge.strategy.prizepicks import ALLOWED
from gridiron_edge.types import Game, PropQuote, Quote

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
BANKROLL = 100_000


def game(i, league="nfl", kickoff=None, status="scheduled"):
    return Game(f"g{i}", league, 2026, 8, kickoff or NOW + timedelta(hours=30),
                f"H{i}", f"A{i}", status=status)


def h2h(gid, home, away, book="pinnacle"):
    return [Quote("odds_api", book, gid, "h2h", "home", None, home, NOW),
            Quote("odds_api", book, gid, "h2h", "away", None, away, NOW)]


def spreads(gid, home_point, dec=1.95, book="pinnacle"):
    return [Quote("odds_api", book, gid, "spreads", "home", home_point, dec, NOW),
            Quote("odds_api", book, gid, "spreads", "away", -home_point, dec, NOW)]


def snap_with(**overrides):
    base = Snapshot(
        now=NOW, games={"g1": game(1)}, quotes=[], props=[], kalshi=[], predictions={},
        gates={}, bankroll_cents=BANKROLL, open_exposure_cents=0, pp_staked_today_cents=0,
        margin_models={"nfl_spread": NormalModel(NFL_SPREAD_SIGMA),
                       "nfl_total": NormalModel(NFL_TOTAL_SIGMA),
                       "cfb": NormalModel(CFB_SIGMA), "nfl_ml": NormalModel(NFL_ML_SIGMA)},
    )
    return replace(base, **overrides)


class StubProjector:
    def __init__(self, projections=None, teams=None):
        self.projections = projections or {}
        self.teams = teams or {}

    def project(self, player, stat, season, asof_week):
        return self.projections.get((player, stat))

    def team_of(self, player):
        return self.teams.get(player)


def _ml_snap(mk, yes_ask, no_ask=None, **kw):
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=yes_ask, no_ask=no_ask, game_id="g1")
    return snap_with(quotes=h2h("g1", 1.80, 2.10), kalshi=[m], **kw)


def test_kalshi_ml_edge_found(mk, settings):
    recs = scan(_ml_snap(mk, 0.48), settings, StubProjector(), lambda m: "nfl")
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "kalshi_game" and r.legs[0].side == "yes" and r.ev > 0.025 and r.stake_cents > 0
    assert r.legs[0].description == "H1 ML" and r.tie_risk and r.expires_at == game(1).kickoff
    assert r.snapshot_ts == NOW and r.stake_cents == round(r.qty * 0.48 * 100)


def test_no_rec_without_edge(mk, settings):
    assert kalshi_game_candidates(_ml_snap(mk, 0.55), settings) == []


def test_takes_no_side_when_yes_overpriced(mk, settings):
    recs = kalshi_game_candidates(_ml_snap(mk, 0.70, no_ask=0.31), settings)
    assert len(recs) == 1 and recs[0].legs[0].side == "no" and recs[0].legs[0].price == 0.31


def test_started_game_skipped(mk, settings):
    snap = _ml_snap(mk, 0.48, games={"g1": game(1, kickoff=NOW - timedelta(minutes=5))})
    assert kalshi_game_candidates(snap, settings) == []
    assert scan(snap, settings, StubProjector(), lambda m: "nfl") == []


def test_observe_only_not_sized(mk, settings):
    snap = _ml_snap(mk, 0.48, gates={("nfl", "ml"): True})
    recs = scan(snap, settings, StubProjector(), lambda m: "nfl")
    assert len(recs) == 1 and recs[0].observe_only and recs[0].stake_cents == 0


def test_spread_uses_distribution(mk, settings):
    # Pinnacle home -3.0 at even no-vig odds: expected home margin 3.0
    def run(yes_ask, no_ask):
        m = mk("E1-H13", "E1", "spread", "H1", line=2.5, yes_ask=yes_ask, no_ask=no_ask, game_id="g1")
        return kalshi_game_candidates(snap_with(quotes=spreads("g1", -3.0), kalshi=[m]), settings)

    recs = run(0.40, 0.61)
    assert len(recs) == 1 and recs[0].legs[0].side == "yes"
    assert recs[0].legs[0].description == "H1 wins by >2.5"
    assert recs[0].legs[0].p_fair == pytest.approx(NormalModel(NFL_SPREAD_SIGMA).p_greater(3.0, 2.5), abs=1e-3)
    assert run(0.60, 0.52) == []


def test_away_spread_uses_negated_margin(mk, settings):
    m = mk("E1-A13", "E1", "spread", "A1", line=2.5, yes_ask=0.20, no_ask=0.81, game_id="g1")
    recs = kalshi_game_candidates(snap_with(quotes=spreads("g1", -3.0), kalshi=[m]), settings)
    assert len(recs) == 1
    assert recs[0].legs[0].p_fair == pytest.approx(NormalModel(NFL_SPREAD_SIGMA).p_greater(-3.0, 2.5), abs=1e-3)


def test_prediction_blends_into_probability(mk, settings):
    pred = Prediction("g1", mean_margin=10.0, mean_total=None, model_version="t")
    base = kalshi_game_candidates(_ml_snap(mk, 0.48), settings)[0].legs[0].p_fair
    blended = kalshi_game_candidates(_ml_snap(mk, 0.48, predictions={"g1": pred}), settings)[0].legs[0].p_fair
    assert blended > base


def test_cfb_model_counts_only_when_it_disagrees(mk, settings):
    g = game(1, league="cfb")
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=0.40, game_id="g1")
    quotes = h2h("g1", 1.80, 2.10) + spreads("g1", -3.0)
    close = Prediction("g1", 4.0, None, "t")
    far = Prediction("g1", 8.0, None, "t")
    # below cfb_min_disagree the model is ignored: a pure Kalshi-vs-book edge, like NFL
    near = kalshi_game_candidates(snap_with(games={"g1": g}, quotes=quotes, kalshi=[m],
                                            predictions={"g1": close}), settings)
    market = kalshi_game_candidates(snap_with(games={"g1": g}, quotes=quotes, kalshi=[m]), settings)
    assert len(near) == 1 and near[0].legs[0].p_fair == pytest.approx(market[0].legs[0].p_fair)
    recs = kalshi_game_candidates(snap_with(games={"g1": g}, quotes=quotes, kalshi=[m],
                                            predictions={"g1": far}), settings)
    assert len(recs) == 1 and not recs[0].tie_risk
    assert recs[0].legs[0].p_fair > near[0].legs[0].p_fair


def test_unmatched_market_ignored(mk, settings):
    m = mk("E9-X", "E9", "ml", "X", yes_ask=0.30, game_id=None)
    assert kalshi_game_candidates(snap_with(quotes=h2h("g1", 1.80, 2.10), kalshi=[m]), settings) == []


def _pp_snap(**kw):
    games, props, teams = {}, [], {}
    for i in range(8):
        gid = f"g{i}"
        games[gid] = game(i)
        player = f"Player {i}"
        teams[player] = "KC" if i % 2 else "BUF"
        props += [PropQuote("pp", "prizepicks", gid, player, "rush_yds", "over", 50.5, None, NOW),
                  PropQuote("odds_api", "draftkings", gid, player, "rush_yds", "over", 50.5, 1.70, NOW),
                  PropQuote("odds_api", "draftkings", gid, player, "rush_yds", "under", 50.5, 2.25, NOW)]
    return snap_with(games=games, props=props, player_teams=teams, **kw)


def test_pp_entry_built(settings):
    recs = pp_entry_candidates(_pp_snap(), settings, StubProjector())
    assert len(recs) == 1
    r = recs[0]
    n, kind = int(r.structure.split("-")[0]), r.structure.split("-")[1]
    assert r.kind == "pp_entry" and 3 <= len(r.legs) <= 6 and (n, kind) in ALLOWED and n == len(r.legs)
    assert all(leg.side == "over" and leg.venue == "prizepicks" for leg in r.legs)
    assert 0 < r.stake_cents <= settings.cap_bet * BANKROLL and r.ev > 0


def test_pp_daily_cap_exhausted(settings):
    snap = _pp_snap(pp_staked_today_cents=int(settings.cap_pp_daily * BANKROLL))
    assert pp_entry_candidates(snap, settings, StubProjector()) == []


def test_pp_pick_without_known_team_skipped(settings):
    snap = _pp_snap()
    # only two players keep a team (one per team): too few picks for any structure
    snap = replace(snap, player_teams={"Player 0": "BUF", "Player 1": "KC"})
    assert pp_entry_candidates(snap, settings, StubProjector()) == []
    teams = {f"Player {i}": ("KC" if i % 2 else "BUF") for i in range(8)}
    assert len(pp_entry_candidates(replace(snap, player_teams={}), settings, StubProjector(teams=teams))) == 1


def test_arb_rec(mk, settings):
    ms = [mk("E1-H1", "E1", "ml", "H1", yes_ask=0.45, game_id="g1"),
          mk("E1-A1", "E1", "ml", "A1", yes_ask=0.45, game_id="g1")]
    recs = arb_candidates(snap_with(kalshi=ms), settings, lambda m: "nfl")
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "arb" and len(r.legs) == 2 and r.tie_risk and r.qty >= 1 and r.ev > 0
    # qty limited by the arb bankroll fraction
    assert r.stake_cents <= settings.arb_fraction * BANKROLL


def test_kalshi_prop_anchored_to_book(mk, settings):
    m = replace(mk("P1", "EP", "prop", line=49.5, yes_ask=0.45, no_ask=0.60, game_id="g1"),
                player="Player 1", stat="rush_yds")
    props = [PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "over", 52.5, 1.70, NOW),
             PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "under", 52.5, 2.25, NOW),
             PropQuote("odds_api", "fanduel", "g1", "Player 1", "rush_yds", "over", 60.5, 1.20, NOW),
             PropQuote("odds_api", "fanduel", "g1", "Player 1", "rush_yds", "under", 60.5, 4.00, NOW)]
    recs = kalshi_prop_candidates(snap_with(kalshi=[m], props=props), settings, StubProjector())
    assert len(recs) == 1 and recs[0].kind == "kalshi_prop" and recs[0].legs[0].side == "yes"
    # nearest book is draftkings at 52.5; shifted down to 49.5 the over is likelier than at 52.5
    assert recs[0].legs[0].p_fair > 0.57


def test_kalshi_prop_model_fallback_needs_extra_margin(mk, settings):
    m = replace(mk("P1", "EP", "prop", line=49.5, yes_ask=0.50, no_ask=0.60, game_id="g1"),
                player="Player 1", stat="rush_yds")
    strong = StubProjector({("Player 1", "rush_yds"): (75.0, 20.0)})
    weak = StubProjector({("Player 1", "rush_yds"): (52.1, 30.0)})
    assert len(kalshi_prop_candidates(snap_with(kalshi=[m]), settings, strong)) == 1
    assert kalshi_prop_candidates(snap_with(kalshi=[m]), settings, weak) == []
    assert kalshi_prop_candidates(snap_with(kalshi=[m]), settings, StubProjector()) == []


def test_slate_caps_hold(mk, settings):
    games, quotes, markets = {}, [], []
    for i in range(15):
        gid = f"g{i}"
        games[gid] = game(i)
        quotes += h2h(gid, 1.50, 2.80)
        markets.append(mk(f"E{i}-H{i}", f"E{i}", "ml", f"H{i}", yes_ask=0.50, game_id=gid))
    recs = scan(snap_with(games=games, quotes=quotes, kalshi=markets), settings,
                StubProjector(), lambda m: "nfl")
    sized = [r for r in recs if r.kind == "kalshi_game"]
    assert len(sized) >= 10
    assert sum(r.stake_cents for r in sized) <= settings.cap_slate * BANKROLL
    assert all(0 < r.stake_cents <= settings.cap_bet * BANKROLL for r in sized)


def _slate_rec(m, p_fair, qty):
    from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev
    from gridiron_edge.types import Leg, Recommendation
    leg = Leg("kalshi", "H1 ML", "yes", m.yes_ask, p_fair, "g1", ticker=m.ticker, team="H1")
    ev = kalshi_ev(p_fair, effective_price(m.yes_ask, qty, m.fee_multiplier))
    return Recommendation(None, "kalshi_game", [leg], 0, qty, ev, p_fair, None, NOW, NOW + timedelta(hours=30))


# 0.50: the final qty fails the min edge; 0.80 (smaller Kelly): it fails only the min EV
@pytest.mark.parametrize("price, p_fair, overrides", [
    (0.50, 0.5335, {"min_stake_cents": 50}),
    (0.80, 0.837, {"kelly_fraction": 0.1, "min_stake_cents": 50}),
])
def test_size_slate_drops_rec_failing_filters_at_final_qty(mk, settings, price, p_fair, overrides):
    from gridiron_edge.pipeline import size_slate
    from gridiron_edge.strategy.kalshi_pricing import effective_price, passes_filters
    settings = replace(settings, **overrides)
    bankroll = 10_000  # $100: provisional qty is tiny, so per-order fee rounding bites
    m = mk("E1-H1", "E1", "ml", "H1", yes_ask=price, game_id="g1")
    prov = int(settings.cap_bet * bankroll / 100 / price)
    p_eff = effective_price(price, prov)
    assert passes_filters(p_fair, price, p_eff, m.yes_ask_size, prov, settings, "active")[0]
    snap = snap_with(kalshi=[m], bankroll_cents=bankroll)
    out = size_slate([_slate_rec(m, p_fair, prov)], snap, settings)
    # Kelly sizes below the provisional qty, the per-contract fee rises and the rec no longer passes
    assert out == []
    # with a clearer edge the same market survives sizing, and its final EV still clears the minimum
    kept = size_slate([_slate_rec(m, p_fair + 0.02, prov)], snap, settings)
    assert len(kept) == 1 and kept[0].ev >= settings.kalshi_min_ev
    k = kept[0]
    assert passes_filters(k.legs[0].p_fair, price, effective_price(price, k.qty), m.yes_ask_size, k.qty,
                          settings, "active")[0]


def test_book_devig_is_worst_case_per_side(settings):
    from gridiron_edge.pipeline import _book_p_sides
    from gridiron_edge.strategy.devig import devig_worst
    props = [PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "over", 50.5, 2.25, NOW),
             PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "under", 50.5, 1.70, NOW)]
    snap = snap_with(props=props)
    p_over, p_under = _book_p_sides(snap, settings, "g1", "Player 1", "rush_yds", 50.5)
    # at the book's own point each side is that side's most conservative devig, not a complement
    assert p_over == pytest.approx(devig_worst([2.25, 1.70], 0), abs=1e-6)
    assert p_under == pytest.approx(devig_worst([2.25, 1.70], 1), abs=1e-6)
    assert p_under < 1.0 - p_over
    # shifted to another line, both sides stay conservative
    o, u = _book_p_sides(snap, settings, "g1", "Player 1", "rush_yds", 47.5)
    assert o + u < 1.0 and o > p_over and u < p_under


def test_pp_under_pick_uses_worst_case_under(settings):
    from gridiron_edge.pipeline import _book_p_sides
    snap = _pp_snap()
    props = []
    for q in snap.props:  # flip the books: every under is now the favourite
        if q.book == "draftkings":
            q = replace(q, decimal=2.25 if q.side == "over" else 1.70)
        props.append(q)
    snap = replace(snap, props=props)
    recs = pp_entry_candidates(snap, settings, StubProjector())
    assert len(recs) == 1
    for leg in recs[0].legs:
        p_over, p_under = _book_p_sides(snap, settings, leg.game_id, leg.player, leg.stat, leg.line)
        assert leg.side == "under" and leg.p_fair == pytest.approx(p_under) and p_under < 1.0 - p_over


def test_open_bet_on_game_blocks_new_kalshi_game_rec(mk, settings):
    snap = _ml_snap(mk, 0.48)
    assert kalshi_game_candidates(snap, settings)
    held = replace(snap, open_game_ids={"g1"})
    assert kalshi_game_candidates(held, settings) == []
    assert not [r for r in scan(held, settings, StubProjector(), lambda m: "nfl") if r.kind == "kalshi_game"]


def test_open_bet_on_player_blocks_new_kalshi_prop_rec(mk, settings):
    m = replace(mk("P1", "EP", "prop", line=49.5, yes_ask=0.45, no_ask=0.60, game_id="g1"),
                player="Player 1", stat="rush_yds")
    props = [PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "over", 52.5, 1.70, NOW),
             PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "under", 52.5, 2.25, NOW)]
    snap = snap_with(kalshi=[m], props=props)
    assert kalshi_prop_candidates(snap, settings, StubProjector())
    assert kalshi_prop_candidates(replace(snap, open_players={"player 1"}), settings, StubProjector()) == []


def test_placed_arb_not_recommended_again(mk, settings):
    ms = [mk("E1-H1", "E1", "ml", "H1", yes_ask=0.45, game_id="g1"),
          mk("E1-A1", "E1", "ml", "A1", yes_ask=0.45, game_id="g1")]
    (rec,) = arb_candidates(snap_with(kalshi=ms), settings, lambda m: "nfl")
    moved = [replace(m, yes_ask=0.44) for m in ms]
    assert arb_candidates(snap_with(kalshi=moved, open_leg_keys={rec.leg_key}), settings, lambda m: "nfl") == []


def test_zero_ask_markets_make_no_arb_and_no_crash(mk, settings):
    pair = [mk("E1-H1", "E1", "ml", "H1", yes_ask=0.0, yes_bid=0.0, game_id="g1"),
            mk("E1-A1", "E1", "ml", "A1", yes_ask=0.0, yes_bid=0.0, game_id="g1")]
    ladder = [mk("E2-H3", "E2", "spread", "H1", line=3.5, yes_ask=0.0, yes_bid=0.0, game_id="g1"),
              mk("E2-H7", "E2", "spread", "H1", line=7.5, yes_ask=0.30, yes_bid=0.20, no_ask=0.0,
                 game_id="g1")]
    from gridiron_edge.strategy.arb import kalshi_ladders, kalshi_ml_pairs
    assert kalshi_ml_pairs(pair, lambda m: "nfl") == [] and kalshi_ladders(ladder) == []
    snap = snap_with(kalshi=pair + ladder, quotes=h2h("g1", 1.80, 2.10))
    assert arb_candidates(snap, settings, lambda m: "nfl") == []
    assert not [r for r in scan(snap, settings, StubProjector(), lambda m: "nfl") if r.kind == "arb"]
