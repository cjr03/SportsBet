"""Quant upgrade: cumulative caps, shrinkage, correlation, CLV throttle, PrizePicks budget,
arb staleness/legging, middles."""
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from gridiron_edge import pipeline
from gridiron_edge.pipeline import (
    Budget,
    arb_candidates,
    direction_key,
    middle_candidates,
    open_exposure,
    pp_entry_candidates,
    scan,
    size_slate,
)
from gridiron_edge.strategy.distributions import NFL_SPREAD_SIGMA, EmpiricalMarginModel
from gridiron_edge.types import Leg, PropQuote
from test_pipeline import BANKROLL, NOW, StubProjector, _pp_snap, game, h2h, snap_with, spreads

NFL = lambda m: "nfl"  # noqa: E731


def _slate(mk, n, start=0, yes_ask=0.50):
    games, quotes, markets = {}, [], []
    for i in range(start, start + n):
        gid = f"g{i}"
        games[gid] = game(i)
        quotes += h2h(gid, 1.50, 2.80)
        markets.append(mk(f"E{i}-H{i}", f"E{i}", "ml", f"H{i}", yes_ask=yes_ask, game_id=gid))
    return games, quotes, markets


def _positions(recs):
    return [(r.kind, r.stake_cents, r.legs) for r in recs if r.stake_cents > 0]


def test_caps_hold_across_scans(mk, settings):
    games, quotes, markets = _slate(mk, 15)
    first = [r for r in scan(snap_with(games=games, quotes=quotes, kalshi=markets), settings, StubProjector(), NFL)
             if r.kind == "kalshi_game"]
    placed = sum(r.stake_cents for r in first)
    assert placed > 0.15 * BANKROLL
    # the same day, fresh games appear; the first slate is PLACED (cash left = bankroll - placed)
    g2, q2, m2 = _slate(mk, 15, start=20)
    allg = {**games, **g2}
    exp = open_exposure(_positions(first), allg)
    snap = snap_with(games=allg, quotes=quotes + q2, kalshi=markets + m2, open_exposure_cents=placed,
                     open_game_ids=set(games), open_game_exposure=exp.game,
                     open_window_exposure=exp.window, open_dir_exposure=exp.direction,
                     placed_today_cents=placed)
    second = [r for r in scan(snap, settings, StubProjector(), NFL) if r.kind == "kalshi_game"]
    assert placed + sum(r.stake_cents for r in second) <= settings.cap_slate * BANKROLL + 1


def test_open_game_exposure_limits_new_bets_on_that_game(mk, settings):
    games, quotes, markets = _slate(mk, 1)
    full = scan(snap_with(games=games, quotes=quotes, kalshi=markets), settings, StubProjector(), NFL)
    assert full and full[0].stake_cents > 0
    used = int(settings.cap_game * BANKROLL) - 300  # $3 of headroom left on g0
    snap = snap_with(games=games, quotes=quotes, kalshi=markets, open_game_exposure={"g0": used},
                     open_exposure_cents=used)
    recs = scan(snap, settings, StubProjector(), NFL)
    assert all(r.stake_cents <= 300 for r in recs)


def test_day_cap(mk, settings):
    games, quotes, markets = _slate(mk, 3)
    snap = snap_with(games=games, quotes=quotes, kalshi=markets,
                     placed_today_cents=int(settings.cap_day * BANKROLL))
    assert [r for r in scan(snap, settings, StubProjector(), NFL) if r.stake_cents > 0] == []


def test_source_shrinkage_and_clv_throttle(mk, settings):
    settings = replace(settings, kelly_fraction=0.1)  # keep the stake below the 2% cap
    games, quotes, markets = _slate(mk, 1, yes_ask=0.60)
    snap = snap_with(games=games, quotes=quotes, kalshi=markets)
    base = scan(snap, settings, StubProjector(), NFL)[0].stake_cents
    assert 0 < base < settings.cap_bet * BANKROLL - 100
    half = scan(replace(snap, clv_mult={"nfl": 0.5}), settings, StubProjector(), NFL)[0].stake_cents
    assert half == pytest.approx(base / 2, rel=0.08)
    shrunk = scan(snap, replace(settings, shrink_game=0.5), StubProjector(), NFL)[0].stake_cents
    assert shrunk == pytest.approx(base / 2, rel=0.08)


def test_direction_keys(settings):
    g = game(1)
    assert direction_key(Leg("kalshi", "", "yes", 0.5, 0.5, "g1", team="H1"), g) == "g1|home"
    assert direction_key(Leg("kalshi", "", "no", 0.5, 0.5, "g1", team="A1"), g) == "g1|home"
    assert direction_key(Leg("kalshi", "", "no", 0.5, 0.5, "g1", team="H1", line=3.5), g) == "g1|away"
    assert direction_key(Leg("kalshi", "", "yes", 0.5, 0.5, "g1", line=44.5), g) == "g1|over"
    assert direction_key(Leg("kalshi", "", "yes", 0.5, 0.5, "g1", player="P", line=49.5, team="A1"), g) == "g1|away"
    assert direction_key(Leg("kalshi", "", "yes", 0.5, 0.5, "g1", player="P", line=49.5), g) is None


def test_same_direction_legs_share_one_cap(mk, settings):
    ml = mk("E1-H1", "E1", "ml", "H1", yes_ask=0.40, game_id="g1")
    prop = replace(mk("P1", "EP", "prop", line=49.5, yes_ask=0.40, no_ask=0.65, game_id="g1"),
                   player="Player 1", stat="rush_yds")
    props = [PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "over", 52.5, 1.60, NOW),
             PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "under", 52.5, 2.40, NOW)]
    snap = snap_with(quotes=h2h("g1", 1.60, 2.50), kalshi=[ml, prop], props=props,
                     player_teams={"Player 1": "H1"})
    loose = [r for r in scan(snap, replace(settings, cap_same_dir=1.0), StubProjector(), NFL) if r.stake_cents > 0]
    assert len(loose) == 2 and {r.legs[0].team for r in loose} == {"H1"}
    assert sum(r.stake_cents for r in loose) > settings.cap_same_dir * BANKROLL
    recs = [r for r in scan(snap, settings, StubProjector(), NFL) if r.stake_cents > 0]
    assert recs and sum(r.stake_cents for r in recs) <= settings.cap_same_dir * BANKROLL + 1


def test_copula_used_for_same_game_legs(mk, settings, monkeypatch):
    seen = {}
    real = pipeline.optimize_slate

    def spy(bets, *a, **kw):
        seen.setdefault("corr", kw.get("corr"))
        return real(bets, *a, **kw)

    monkeypatch.setattr(pipeline, "optimize_slate", spy)
    test_same_direction_legs_share_one_cap(mk, settings)
    c = np.asarray(seen["corr"])
    assert c.shape == (2, 2) and c[0, 1] == pytest.approx(settings.copula_rho)


def test_fee_odds_reoptimised_at_final_qty(mk, settings, monkeypatch):
    calls = []
    real = pipeline.optimize_slate

    def spy(bets, *a, **kw):
        calls.append([b.d for b in bets])
        return real(bets, *a, **kw)

    monkeypatch.setattr(pipeline, "optimize_slate", spy)
    games, quotes, markets = _slate(mk, 3, yes_ask=0.30)
    snap = snap_with(games=games, quotes=quotes, kalshi=markets, bankroll_cents=20_000)
    kalshi = pipeline.kalshi_game_candidates(snap, settings)
    out = size_slate(kalshi, snap, settings)
    assert len(calls) == 2  # a second pass re-sizes the survivors at their final-quantity fee
    from gridiron_edge.strategy.kalshi_pricing import effective_price
    for r in out:
        assert r.ev == pytest.approx(r.legs[0].p_fair / effective_price(0.30, r.qty) - 1)


def test_kalshi_prop_leg_carries_player_team_and_fee_multiplier(mk, settings):
    m = replace(mk("P1", "EP", "prop", line=49.5, yes_ask=0.45, no_ask=0.60, game_id="g1"),
                player="Player 1", stat="rush_yds", fee_multiplier=0.5)
    props = [PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "over", 52.5, 1.70, NOW),
             PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "under", 52.5, 2.25, NOW)]
    (r,) = pipeline.kalshi_prop_candidates(snap_with(kalshi=[m], props=props, player_teams={"Player 1": "A1"}),
                                           settings, StubProjector())
    assert r.legs[0].team == "A1" and r.legs[0].fee_multiplier == 0.5


def test_ml_tie_risk_follows_rules(mk, settings):
    m = replace(mk("E1-H1", "E1", "ml", "H1", yes_ask=0.48, game_id="g1"),
                rules_primary="If the game ends in a tie, the market resolves to a 50/50 split.")
    (r,) = pipeline.kalshi_game_candidates(snap_with(quotes=h2h("g1", 1.80, 2.10), kalshi=[m]), settings)
    assert not r.tie_risk


# -- PrizePicks ------------------------------------------------------------------------------

def test_pp_stake_uses_available_bankroll_and_unverified_multiplier(settings):
    s = replace(settings, pp_max_entries=1)
    base = pp_entry_candidates(_pp_snap(), s, StubProjector())[0].stake_cents
    verified = pp_entry_candidates(_pp_snap(), replace(s, pp_payouts_verified=True), StubProjector())[0]
    assert verified.stake_cents == pytest.approx(2 * base, abs=2)
    half_open = _pp_snap(open_exposure_cents=BANKROLL // 2)
    assert pp_entry_candidates(half_open, s, StubProjector())[0].stake_cents == pytest.approx(base / 2, abs=2)


def test_pp_multiple_non_overlapping_entries(settings):
    snap = _pp_snap()
    recs = pp_entry_candidates(snap, replace(settings, pp_max_entries=3), StubProjector())
    assert len(recs) >= 1
    games = [leg.game_id for r in recs for leg in r.legs]
    assert len(games) == len(set(games))
    big = replace(snap, games={**snap.games, **{f"g{i}": game(i) for i in range(8, 16)}},
                  props=snap.props + [replace(q, game_id=f"g{int(q.game_id[1:]) + 8}", player=q.player + "b")
                                      for q in snap.props],
                  player_teams={**snap.player_teams, **{p + "b": t for p, t in snap.player_teams.items()}})
    recs = pp_entry_candidates(big, replace(settings, pp_max_entries=3), StubProjector())
    assert len(recs) >= 2
    games = [leg.game_id for r in recs for leg in r.legs]
    assert len(games) == len(set(games))


def test_pp_open_entry_players_and_games_excluded(settings):
    snap = _pp_snap()
    (r,) = pp_entry_candidates(snap, replace(settings, pp_max_entries=1), StubProjector())
    held = replace(snap, pp_open_players={leg.player.lower() for leg in r.legs},
                   pp_open_games={leg.game_id for leg in r.legs})
    for rec in pp_entry_candidates(held, settings, StubProjector()):
        assert not {leg.game_id for leg in rec.legs} & {leg.game_id for leg in r.legs}


def test_pp_multiplier_guard(settings):
    snap = _pp_snap()
    odd = [replace(q, multiplier=1.5) if q.book == "prizepicks" else q for q in snap.props]
    assert pp_entry_candidates(replace(snap, props=odd), settings, StubProjector()) == []
    one = [replace(q, multiplier=1.0) if q.book == "prizepicks" else q for q in snap.props]
    assert pp_entry_candidates(replace(snap, props=one), settings, StubProjector())


def test_pp_charged_against_game_headroom(settings):
    snap = _pp_snap()
    full = {f"g{i}": int(settings.cap_game * BANKROLL) for i in range(8)}
    assert pp_entry_candidates(replace(snap, open_game_exposure=full), settings, StubProjector()) == []


def test_pp_arena_mode_reports_perfect_only_odds(settings):
    recs = pp_entry_candidates(_pp_snap(), replace(settings, pp_mode="arena"), StubProjector())
    assert all(r.structure.endswith("power") for r in recs)


# -- arbs and middles ----------------------------------------------------------------------------

def _pair(mk, **kw):
    return [mk("E1-H1", "E1", "ml", "H1", yes_ask=0.45, game_id="g1", yes_ask_size=500),
            mk("E1-A1", "E1", "ml", "A1", yes_ask=0.45, game_id="g1", yes_ask_size=80, **kw)]


def test_stale_arb_dropped(mk, settings):
    ms = [replace(m, fetched_at=NOW - timedelta(seconds=settings.arb_stale_seconds + 1)) for m in _pair(mk)]
    assert arb_candidates(snap_with(kalshi=ms), settings, NFL) == []
    assert arb_candidates(snap_with(kalshi=_pair(mk)), settings, NFL)


def test_arb_min_roi(mk, settings):
    assert arb_candidates(snap_with(kalshi=_pair(mk)), replace(settings, arb_min_roi=0.2), NFL) == []


def test_arb_legging_guidance(mk, settings):
    (r,) = arb_candidates(snap_with(kalshi=_pair(mk)), settings, NFL)
    first, second = r.legs
    assert first.ticker == "E1-A1"  # the thinner side is filled first
    assert first.worst_price == first.price
    from gridiron_edge.strategy.kalshi_pricing import taker_fee
    w = second.worst_price
    assert w is not None and w > second.price
    cost = r.qty * (first.price + w) + taker_fee(r.qty, first.price) + taker_fee(r.qty, w)
    assert cost <= r.qty + 1e-9
    w2 = w + 0.01
    assert r.qty * (first.price + w2) + taker_fee(r.qty, first.price) + taker_fee(r.qty, w2) > r.qty


def _middle_snap(mk, model):
    ladder = [mk("E2-H13", "E2", "spread", "H1", line=2.5, yes_ask=0.52, no_ask=0.49, game_id="g1"),
              mk("E2-H14", "E2", "spread", "H1", line=3.5, yes_ask=0.51, no_ask=0.50, game_id="g1")]
    snap = snap_with(kalshi=ladder, quotes=spreads("g1", -3.0))
    return replace(snap, margin_models={**snap.margin_models, "nfl_spread": model})


def test_middles_need_empirical_model_and_are_observe_only(mk, settings):
    assert middle_candidates(_middle_snap(mk, snap_with().margin_models["nfl_spread"]), settings) == []
    resid = np.concatenate([np.full(300, 0), np.full(80, 4), np.full(80, -4), np.arange(-20, 21).repeat(4)])
    model = EmpiricalMarginModel(resid, NFL_SPREAD_SIGMA)
    (r,) = middle_candidates(_middle_snap(mk, model), settings)
    assert r.kind == "middle" and r.observe_only and r.stake_cents == 0 and 0 < r.p_win < 1
    assert r.ev > 0 and not r.tie_risk
    recs = scan(_middle_snap(mk, model), settings, StubProjector(), NFL)
    assert [x for x in recs if x.kind == "middle"] and not [x for x in recs if x.kind == "arb"]


def test_live_middles_are_sized(mk, settings):
    resid = np.concatenate([np.full(300, 0), np.full(80, 4), np.full(80, -4), np.arange(-20, 21).repeat(4)])
    snap = _middle_snap(mk, EmpiricalMarginModel(resid, NFL_SPREAD_SIGMA))
    (r,) = middle_candidates(snap, replace(settings, middles_live=True), Budget.from_snap(snap, settings))
    assert not r.observe_only and 0 < r.stake_cents <= settings.cap_bet * BANKROLL and r.qty >= 1
