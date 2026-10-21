"""Kalshi prop pricing fixes: median-dependent yardage spread, relative translation cap, shrink to the
Kalshi mid, plausibility cap, projector-only never texted, honest p_mkt, nflverse name collisions.

Regression fixtures are the real bad recs from the live DB (Raridon, Kmet, Kupp, J. Jefferson)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest
from scipy.stats import gamma

from gridiron_edge.pipeline import Snapshot, _book_p_sides, _shrink_to_market, kalshi_prop_candidates
from gridiron_edge.strategy.distributions import NormalModel, implied_mean_from_line, prop_cv, prop_p_over
from gridiron_edge.types import Game, PropQuote

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)


def snap_with(**kw):
    base = Snapshot(now=NOW, games={"g1": Game("g1", "nfl", 2026, 8, NOW + timedelta(hours=30), "CHI", "NE")},
                    quotes=[], props=[], kalshi=[], predictions={}, gates={}, bankroll_cents=100_000,
                    open_exposure_cents=0, pp_staked_today_cents=0,
                    margin_models={k: NormalModel(13.0) for k in ("nfl_spread", "nfl_total", "nfl_ml", "cfb")})
    return replace(base, **kw)


class Stub:
    def __init__(self, projections=None):
        self.projections = projections or {}

    def project(self, player, stat, season, asof_week):
        return self.projections.get((player, stat))

    def team_of(self, player):
        return None


def book(point, over=1.90, under=1.90, player="Player 1", stat="reception_yds", bk="draftkings"):
    return [PropQuote("odds_api", bk, "g1", player, stat, "over", point, over, NOW),
            PropQuote("odds_api", bk, "g1", player, stat, "under", point, under, NOW)]


def market(mk, line, yes_bid, yes_ask, player="Player 1", stat="reception_yds", **kw):
    m = mk("P1", "EP", "prop", line=line, yes_bid=yes_bid, yes_ask=yes_ask, no_ask=round(1 - yes_bid, 4),
           game_id="g1", **kw)
    return replace(m, player=player, stat=stat)


def _p_at(line, strike, stat="reception_yds", p=0.5):
    mu = implied_mean_from_line(line, p, "yards", stat=stat)
    return prop_p_over(strike, mu, "yards", stat=stat, anchor=line)


# -- 1. spread that depends on the median ---------------------------------------------------------------

def test_kmet_tail_from_a_low_line_is_not_three_percent():
    # Cole Kmet: books 9.5 at ~50/50; the app said P(25+) = 2.6%, Kalshi traded 16-19%
    assert 0.15 <= _p_at(9.5, 24.5) <= 0.25


def test_raridon_tail_from_book_line_near_kalshi():
    # Eli Raridon: books 14.5 at ~50/50; the app said 48% (older code) / 15% (branch); Kalshi 25-29c
    assert 0.25 <= _p_at(14.5, 24.5) <= 0.35


def test_mid_receiving_line_close_to_old_gamma():
    from scipy.optimize import brentq
    k = 1 / 0.55 ** 2  # the old fixed receiving CV
    m0 = brentq(lambda m: gamma.sf(59.5, k, scale=m / k) - 0.5, 1, 500)
    for strike in (44.5, 74.5):
        assert _p_at(59.5, strike) == pytest.approx(float(gamma.sf(strike, k, scale=m0 / k)), abs=0.02)


def test_prop_cv_shape():
    assert prop_cv("pass_yds", 250.5) == pytest.approx(0.30)
    assert prop_cv("reception_yds", 20.0) == pytest.approx(0.835)
    assert prop_cv("rush_yds", 40.0) == pytest.approx(0.67)
    assert prop_cv("reception_yds", 9.5) > 1.0 > prop_cv("reception_yds", 45.5)  # spread falls with the median
    assert prop_cv("reception_yds", 0.5) == pytest.approx(1.6)  # capped
    assert prop_cv("rush_yds", 0.5) <= 1.6


def test_fit_prop_cv_recovers_a_known_law():
    from gridiron_edge.models.props import fit_prop_cv
    rng = np.random.default_rng(3)
    rows = []
    for i in range(160):
        med = 6 + i * 0.6  # medians 6..102
        cv = min(0.835 * (med / 20) ** -0.41, 1.6)
        k = 1 / cv ** 2
        scale = med / gamma.median(k)
        for season in (2024, 2025):
            for wk, y in enumerate(gamma.rvs(k, scale=scale, size=17, random_state=rng), start=1):
                rows.append({"player_id": f"p{i}", "player_display_name": f"P {i}", "position": "WR",
                             "season": season, "week": wk, "receiving_yards": float(round(y))})
    fit = fit_prop_cv(pl.DataFrame(rows))
    a, ref, b = fit["reception_yds"]
    assert ref == 20.0 and -0.6 < b < -0.25 and 0.65 < a < 1.05


# -- 2. translation distance is relative to the line -------------------------------------------------

def test_kmet_strike_too_far_from_low_line(settings):
    snap = snap_with(props=book(9.5, player="Cole Kmet"))
    assert _book_p_sides(snap, settings, "g1", "Cole Kmet", "reception_yds", 24.5) is None
    assert _book_p_sides(snap, settings, "g1", "Cole Kmet", "reception_yds", 14.5) is not None  # 5-yd floor
    hi = snap_with(props=book(60.5))
    assert _book_p_sides(hi, settings, "g1", "Player 1", "reception_yds", 75.5) is not None
    pas = snap_with(props=book(245.5, stat="pass_yds"))
    assert _book_p_sides(pas, settings, "g1", "Player 1", "pass_yds", 275.5) is not None


# -- 3./4. shrink to the Kalshi mid, plausibility cap ---------------------------------------------------

def test_shrink_to_market_formula(mk):
    m = market(mk, 24.5, 0.25, 0.26)
    po, pu = _shrink_to_market(0.30, 0.68, m, 0.5)
    assert po == pytest.approx(0.5 * 0.30 + 0.5 * 0.255)
    assert pu == pytest.approx(0.5 * 0.68 + 0.5 * 0.745)
    assert _shrink_to_market(0.30, 0.68, market(mk, 24.5, 0.20, 0.27), 0.5) is None  # spread 7c
    assert _shrink_to_market(0.30, 0.68, replace(m, no_ask_size=0), 0.5) is None  # one-sided book
    assert _shrink_to_market(0.30, 0.68, replace(m, yes_bid=0.0), 0.5) is None  # no bid


def test_raridon_regression_no_yes_text_at_48(mk, settings):
    # rec #116/184: YES Raridon 25+ at 29c with p 48%; books had him at 14.5
    m = market(mk, 24.5, 0.28, 0.29, player="Eli Raridon")
    recs = kalshi_prop_candidates(snap_with(kalshi=[m], props=book(14.5, player="Eli Raridon")), settings, Stub())
    assert not [r for r in recs if r.legs[0].side == "yes"]
    proj = Stub({("Eli Raridon", "reception_yds"): (30.0, 15.0)})  # the old 48% projector call
    assert kalshi_prop_candidates(snap_with(kalshi=[m]), settings, proj) == []


def test_kmet_regression_no_text(mk, settings):
    m = market(mk, 24.5, 0.16, 0.19, player="Cole Kmet")
    assert kalshi_prop_candidates(snap_with(kalshi=[m], props=book(9.5, player="Cole Kmet")), settings, Stub()) == []


def test_implausible_projector_gap_dropped(mk, settings):
    # Cooper Kupp 15+: projector 87.7% vs Kalshi 64%
    m = market(mk, 14.5, 0.63, 0.65, player="Cooper Kupp")
    kupp = Stub({("Cooper Kupp", "reception_yds"): (40.0, 12.0)})
    assert kalshi_prop_candidates(snap_with(kalshi=[m]), settings, kupp) == []


def test_wide_or_empty_kalshi_book_skipped(mk, settings):
    props = book(52.5, 1.70, 2.25, stat="rush_yds")
    wide = market(mk, 52.5, 0.40, 0.48, stat="rush_yds")
    assert kalshi_prop_candidates(snap_with(kalshi=[wide], props=props), settings, Stub()) == []


def test_exact_strike_anchored_is_live_and_shrunk(mk, settings):
    props = book(52.5, 1.70, 2.25, stat="rush_yds")
    m = market(mk, 52.5, 0.47, 0.49, stat="rush_yds")
    (r,) = kalshi_prop_candidates(snap_with(kalshi=[m], props=props), settings, Stub())
    leg = r.legs[0]
    assert leg.side == "yes" and not r.observe_only
    p_book = 0.569620253164557
    assert leg.p_fair == pytest.approx(0.8 * p_book + 0.2 * 0.48, abs=1e-6)
    assert leg.p_mkt == pytest.approx(p_book, abs=1e-6)  # the books priced this exact line
    assert r.why["kalshi_mid"] == pytest.approx(0.48) and r.why["weight"] == 0.8


def test_translated_strike_has_no_market_p(mk, settings):
    props = book(52.5, 3.60, 1.30, stat="rush_yds")  # books ~25% over 52.5: ~22% over 55.5
    m = market(mk, 55.5, 0.14, 0.15, stat="rush_yds")
    (r,) = kalshi_prop_candidates(snap_with(kalshi=[m], props=props), settings, Stub())
    assert r.legs[0].side == "yes" and r.legs[0].p_mkt is None and r.p_mkt is None
    assert r.why["kalshi_mid"] == pytest.approx(0.145) and r.why["book_line"] == 52.5
    assert r.why["weight"] == pytest.approx(0.5 * np.exp(-3 / 52.5))
    assert 0.145 < r.legs[0].p_fair < r.why["p_ours"]  # pulled most of the way back to the mid


# -- 5. projector-only is never texted ------------------------------------------------------------------

def test_projector_only_always_observe(mk, settings):
    m = market(mk, 49.5, 0.45, 0.46, stat="rush_yds")
    proj = Stub({("Player 1", "rush_yds"): (52.0, 25.0)})
    settings = replace(settings, kalshi_min_ev=-1.0, kalshi_min_edge=-1.0)  # any edge: we test the flag
    for snap in (snap_with(kalshi=[m], projector_live=True),
                 snap_with(kalshi=[m], projector_live=True, gates={("nfl", "prop_model"): False})):
        recs = kalshi_prop_candidates(snap, settings, proj)
        assert recs and all(r.observe_only for r in recs)


# -- 7. nflverse name collisions -------------------------------------------------------------------------

def _jefferson_weekly():
    rows = [{"player_id": "00-0036322", "player_display_name": "Justin Jefferson", "position": "WR",
             "team": "MIN", "season": 2026, "week": w, "targets": 9.0, "receiving_yards": float(y),
             "opponent_team": "GB"} for w, y in zip(range(1, 7), (92, 55, 32, 110, 79, 101))]
    rows.append({"player_id": "00-0041075", "player_display_name": "Justin Jefferson", "position": "LB",
                 "team": "CLE", "season": 2026, "week": 7, "targets": 0.0, "receiving_yards": 0.0,
                 "opponent_team": "PIT"})
    return pl.DataFrame(rows)


def test_name_collision_resolves_to_the_receiver():
    from gridiron_edge.models.props import PropProjector
    p = PropProjector(_jefferson_weekly(), usage_model=False)
    assert p.team_of("Justin Jefferson") == "MIN"
    mean, _ = p.project("Justin Jefferson", "reception_yds", 2026, 8)
    assert mean > 60  # the WR's ~78 yards, not merged with the LB's zero


def test_name_collision_injury_and_snaps_by_id_and_team():
    from gridiron_edge.models.props import PropProjector
    inj = pl.DataFrame({"gsis_id": ["00-0041075"], "full_name": ["Justin Jefferson"], "season": [2026],
                        "week": [8], "report_status": ["Out"]})
    snaps = pl.DataFrame({"player": ["Justin Jefferson"] * 2, "team": ["MIN", "CLE"], "season": [2026] * 2,
                          "week": [3, 3], "offense_pct": [0.9, 0.0]})
    p = PropProjector(_jefferson_weekly(), snaps=snaps, injuries=inj, usage_model=False)
    assert not p.is_out("Justin Jefferson", 2026, 8)  # the LB is out, not the WR
    assert p.project("Justin Jefferson", "reception_yds", 2026, 8) is not None
    # the LB's 0% snap row did not drop the WR's week 3 (joined on team)
    h = p._hist["00-0036322"]
    assert not np.isnan(h.snap).all() and np.nanmax(h.snap) == pytest.approx(0.9)


def test_why_explains_the_pull_to_kalshi(mk, settings):
    from gridiron_edge.describe import why_lines
    props = book(52.5, 1.70, 2.25, stat="rush_yds")
    (r,) = kalshi_prop_candidates(snap_with(kalshi=[market(mk, 52.5, 0.47, 0.49, stat="rush_yds")], props=props),
                                  settings, Stub())
    text = "\n".join(why_lines(r, NOW))
    assert "Kalshi traders say 48%; our own estimate (57%) counts for 80% of our number" in text
