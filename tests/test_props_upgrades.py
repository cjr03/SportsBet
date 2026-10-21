"""Quant-audit props upgrades: anchor staleness, projector gate, PP multipliers, consensus, shift cap."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gridiron_edge.pipeline import (
    Snapshot,
    _book_p_sides,
    kalshi_prop_candidates,
    pp_entry_candidates,
    scan,
)
from gridiron_edge.strategy.devig import devig_worst
from gridiron_edge.strategy.distributions import NormalModel
from gridiron_edge.types import Game, PropQuote

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
BANKROLL = 100_000


def game(i):
    return Game(f"g{i}", "nfl", 2026, 8, NOW + timedelta(hours=30), f"H{i}", f"A{i}")


def snap_with(**kw):
    base = Snapshot(now=NOW, games={"g1": game(1)}, quotes=[], props=[], kalshi=[], predictions={},
                    gates={}, bankroll_cents=BANKROLL, open_exposure_cents=0, pp_staked_today_cents=0,
                    margin_models={k: NormalModel(13.0) for k in ("nfl_spread", "nfl_total", "nfl_ml", "cfb")})
    return replace(base, **kw)


class Stub:
    def __init__(self, projections=None, teams=None):
        self.projections = projections or {}
        self.teams = teams or {}

    def project(self, player, stat, season, asof_week):
        return self.projections.get((player, stat))

    def team_of(self, player):
        return self.teams.get(player)


def book(bk, over, under, point=52.5, player="Player 1", gid="g1", stamp=NOW, fetched=None, stat="rush_yds"):
    return [PropQuote("odds_api", bk, gid, player, stat, "over", point, over, stamp, fetched_at=fetched),
            PropQuote("odds_api", bk, gid, player, stat, "under", point, under, stamp, fetched_at=fetched)]


def prop_market(mk, line=49.5, yes_ask=0.45, no_ask=0.60, player="Player 1"):
    return replace(mk("P1", "EP", "prop", line=line, yes_ask=yes_ask, no_ask=no_ask, game_id="g1"),
                   player=player, stat="rush_yds")


# -- 1. anchor age from fetch time, longer TTL, no projector fallback when a book quote exists ----

def test_book_quote_aged_from_fetch_time_not_book_stamp(settings):
    # the book last moved its line 2 h ago, but we fetched it 5 min ago: still a valid anchor
    props = book("draftkings", 1.70, 2.25, stamp=NOW - timedelta(hours=2), fetched=NOW - timedelta(minutes=5))
    assert _book_p_sides(snap_with(props=props), settings, "g1", "Player 1", "rush_yds", 52.5) is not None


def test_book_quote_older_than_props_ttl_is_dropped(settings):
    old = NOW - timedelta(seconds=settings.props_stale_seconds + 60)
    props = book("draftkings", 1.70, 2.25, stamp=old, fetched=old)
    assert _book_p_sides(snap_with(props=props), settings, "g1", "Player 1", "rush_yds", 52.5) is None


def test_props_ttl_defaults_to_hours(settings):
    assert settings.props_stale_seconds >= 2 * 3600


def test_no_projector_fallback_when_fresh_book_quote_exists(mk, settings):
    # only an over is quoted: no anchor can be built, but a book prices this player - don't use the model
    props = [PropQuote("odds_api", "draftkings", "g1", "Player 1", "rush_yds", "over", 52.5, 1.70, NOW)]
    strong = Stub({("Player 1", "rush_yds"): (90.0, 20.0)})
    assert kalshi_prop_candidates(snap_with(kalshi=[prop_market(mk)], props=props), settings, strong) == []
    # and with no book quote at all the projector still prices it
    assert len(kalshi_prop_candidates(snap_with(kalshi=[prop_market(mk)]), settings, strong)) == 1


def test_stale_prizepicks_quote_skipped(settings):
    snap = _pp_snap()
    old = NOW - timedelta(seconds=settings.props_stale_seconds + 60)
    aged = [replace(q, last_update=old, fetched_at=old) if q.book == "prizepicks" else q for q in snap.props]
    assert pp_entry_candidates(replace(snap, props=aged), settings, Stub()) == []
    # a hand-typed line is the user's own call: never aged out
    manual = [replace(q, source="manual", last_update=old) if q.book == "prizepicks" else q for q in snap.props]
    assert len(pp_entry_candidates(replace(snap, props=manual), settings, Stub())) == 1


# -- 2. projector-only props are observe-only until the projector passes its backtest -------------

def test_projector_only_kalshi_prop_is_observe_only(mk, settings):
    strong = Stub({("Player 1", "rush_yds"): (90.0, 20.0)})
    recs = kalshi_prop_candidates(snap_with(kalshi=[prop_market(mk)]), settings, strong)
    assert len(recs) == 1 and recs[0].observe_only
    live = kalshi_prop_candidates(snap_with(kalshi=[prop_market(mk)], projector_live=True), settings, strong)
    assert len(live) == 1 and not live[0].observe_only


def test_anchored_kalshi_prop_stays_live(mk, settings):
    recs = kalshi_prop_candidates(snap_with(kalshi=[prop_market(mk)], props=book("draftkings", 1.70, 2.25)),
                                  settings, Stub())
    assert len(recs) == 1 and not recs[0].observe_only


def test_projector_only_prop_not_sized_by_scan(mk, settings):
    strong = Stub({("Player 1", "rush_yds"): (90.0, 20.0)})
    recs = [r for r in scan(snap_with(kalshi=[prop_market(mk)]), settings, strong, lambda m: "nfl")
            if r.kind == "kalshi_prop"]
    assert len(recs) == 1 and recs[0].observe_only and recs[0].stake_cents == 0


def _pp_snap(n=8, books=True, **kw):
    games, props, teams = {}, [], {}
    for i in range(n):
        gid = f"g{i}"
        games[gid] = game(i)
        player = f"Player {i}"
        teams[player] = "KC" if i % 2 else "BUF"
        props.append(PropQuote("odds_api", "prizepicks", gid, player, "rush_yds", "over", 50.5, None, NOW))
        if books:
            props += book("draftkings", 1.70, 2.25, point=50.5, player=player, gid=gid)
    return snap_with(games=games, props=props, player_teams=teams, **kw)


def test_projector_only_pp_entry_is_observe_only(settings):
    proj = Stub({(f"Player {i}", "rush_yds"): (80.0, 15.0) for i in range(8)})
    recs = pp_entry_candidates(_pp_snap(books=False), settings, proj)
    assert len(recs) == 1 and recs[0].observe_only and recs[0].stake_cents == 0
    live = pp_entry_candidates(_pp_snap(books=False, projector_live=True), settings, proj)
    assert len(live) == 1 and not live[0].observe_only and live[0].stake_cents > 0


def test_pp_entry_prefers_anchored_legs_for_a_live_entry(settings):
    snap = _pp_snap()
    # two extra players with only a projection (and very confident ones)
    extra = {}
    for i in (8, 9):
        snap.games[f"g{i}"] = game(i)
        snap.player_teams[f"Player {i}"] = "KC" if i % 2 else "BUF"
        snap.props.append(PropQuote("odds_api", "prizepicks", f"g{i}", f"Player {i}", "rush_yds", "over", 50.5,
                                    None, NOW))
        extra[(f"Player {i}", "rush_yds")] = (120.0, 10.0)
    recs = pp_entry_candidates(snap, settings, Stub(extra))
    assert len(recs) == 1 and not recs[0].observe_only
    assert {leg.player for leg in recs[0].legs}.isdisjoint({"Player 8", "Player 9"})


# -- 3. PrizePicks non-standard (Demon/Goblin) lines are excluded -----------------------------------

def test_pp_nonstandard_multiplier_lines_skipped(settings):
    snap = _pp_snap()
    demon = [replace(q, multiplier=1.5) if q.book == "prizepicks" else q for q in snap.props]
    assert pp_entry_candidates(replace(snap, props=demon), settings, Stub()) == []
    standard = [replace(q, multiplier=1.0) if q.book == "prizepicks" else q for q in snap.props]
    assert len(pp_entry_candidates(replace(snap, props=standard), settings, Stub())) == 1


# -- 5. consensus anchor across books --------------------------------------------------------------

def test_anchor_is_median_of_books_not_alphabetical(settings):
    props = (book("betmgm", 1.50, 2.70) + book("draftkings", 1.90, 1.90) + book("fanduel", 1.92, 1.88))
    p_over, p_under = _book_p_sides(snap_with(props=props), settings, "g1", "Player 1", "rush_yds", 52.5)
    overs = sorted(devig_worst([o, u], 0) for o, u in ((1.50, 2.70), (1.90, 1.90), (1.92, 1.88)))
    unders = sorted(devig_worst([o, u], 1) for o, u in ((1.50, 2.70), (1.90, 1.90), (1.92, 1.88)))
    assert p_over == pytest.approx(overs[1], abs=1e-6)
    assert p_under == pytest.approx(unders[1], abs=1e-6)


def test_anchor_uses_books_at_nearest_point_only(settings):
    props = book("draftkings", 1.90, 1.90, point=52.5) + book("betmgm", 1.20, 4.00, point=60.5)
    p_over, _ = _book_p_sides(snap_with(props=props), settings, "g1", "Player 1", "rush_yds", 52.5)
    assert p_over == pytest.approx(devig_worst([1.90, 1.90], 0), abs=1e-6)


# -- 4. capped shift distance ------------------------------------------------------------------------

def test_strike_far_from_book_point_not_priced(mk, settings):
    props = book("draftkings", 1.90, 1.90, point=52.5)
    near = _book_p_sides(snap_with(props=props), settings, "g1", "Player 1", "rush_yds", 52.5 + 10)
    far = _book_p_sides(snap_with(props=props), settings, "g1", "Player 1", "rush_yds",
                        52.5 + settings.props_max_shift_yds + 5)
    assert near is not None and far is None
    # pass yards get a wider window
    pass_props = book("draftkings", 1.90, 1.90, point=245.5, stat="pass_yds")
    assert _book_p_sides(snap_with(props=pass_props), settings, "g1", "Player 1", "pass_yds", 245.5 + 25) is not None


# -- 4. skewed yardage, count push handling ------------------------------------------------------

def test_yards_distribution_is_right_skewed():
    from gridiron_edge.strategy.distributions import implied_mean_from_line, prop_p_over
    mu = implied_mean_from_line(85.5, 0.5, "yards", stat="reception_yds")
    # median pinned at the line, mean above it (right skew)
    assert mu > 85.5
    up = prop_p_over(85.5 + 15, mu, "yards", stat="reception_yds")
    down = 1 - prop_p_over(85.5 - 15, mu, "yards", stat="reception_yds")
    assert up > down  # the long right tail
    # never negative yardage mass: P(over 0.5) is near 1 for a sizeable mean
    assert prop_p_over(0.5, mu, "yards", stat="reception_yds") > 0.99


def test_pass_yards_tighter_than_receiving():
    from gridiron_edge.strategy.distributions import implied_mean_from_line, prop_p_over
    mp = implied_mean_from_line(245.5, 0.5, "yards", stat="pass_yds")
    mr = implied_mean_from_line(245.5, 0.5, "yards", stat="reception_yds")
    assert prop_p_over(299.5, mp, "yards", stat="pass_yds") < prop_p_over(299.5, mr, "yards", stat="reception_yds")


def test_projector_sd_gives_gamma_with_that_spread():
    from scipy.stats import gamma
    from gridiron_edge.strategy.distributions import prop_p_over
    mean, sd = 70.0, 30.0
    k = (mean / sd) ** 2
    assert prop_p_over(60.5, mean, "yards", sd) == pytest.approx(gamma.sf(60.5, k, scale=mean / k))


def test_count_integer_line_push_is_not_an_over():
    from scipy.stats import poisson
    from gridiron_edge.strategy.distributions import prop_p_over
    assert prop_p_over(4.0, 4.2, "count") == pytest.approx(poisson.sf(4, 4.2))
    assert prop_p_over(4.5, 4.2, "count") == pytest.approx(poisson.sf(4, 4.2))


def test_overdispersed_count_uses_negative_binomial():
    from scipy.stats import poisson
    from gridiron_edge.strategy.distributions import prop_p_over
    # variance 9 > mean 4: fatter tails than Poisson
    assert prop_p_over(8.5, 4.0, "count", 3.0) > poisson.sf(8, 4.0)
    # sd at or below sqrt(mean): Poisson
    assert prop_p_over(8.5, 4.0, "count", 1.5) == pytest.approx(poisson.sf(8, 4.0))


# -- 9. game context reaches a projector that accepts it ---------------------------------------------

def test_projector_gets_opponent_and_market_game_script(mk, settings):
    from gridiron_edge.types import Quote
    seen = {}

    class Ctx(Stub):
        def project(self, player, stat, season, asof_week, *, opponent=None, team_total=None, team_spread=None):
            seen.update(opponent=opponent, team_total=team_total, team_spread=team_spread)
            return (90.0, 20.0)

    quotes = [Quote("odds_api", "pinnacle", "g1", "spreads", "home", -7.0, 1.95, NOW),
              Quote("odds_api", "pinnacle", "g1", "spreads", "away", 7.0, 1.95, NOW),
              Quote("odds_api", "pinnacle", "g1", "totals", "over", 45.0, 1.95, NOW),
              Quote("odds_api", "pinnacle", "g1", "totals", "under", 45.0, 1.95, NOW)]
    snap = snap_with(kalshi=[prop_market(mk)], quotes=quotes, player_teams={"Player 1": "H1"})
    assert len(kalshi_prop_candidates(snap, settings, Ctx())) == 1
    assert seen["opponent"] == "A1"
    assert seen["team_spread"] == pytest.approx(-7.0, abs=0.01)
    assert seen["team_total"] == pytest.approx(26.0, abs=0.01)
