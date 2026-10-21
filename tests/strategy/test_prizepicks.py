import pytest

from gridiron_edge.strategy.prizepicks import (
    ALLOWED,
    Pick,
    breakeven,
    build_best_entry,
    entry_ev,
    hit_distribution,
    kelly_entry,
    revert,
)


@pytest.mark.parametrize("st,be", [((2,"power"),.5774),((3,"power"),.5503),((4,"power"),.5623),((5,"power"),.5493),((6,"power"),.5466),
                                   ((2,"flex"),.6180),((3,"flex"),.5774),((4,"flex"),.5503),((5,"flex"),.5425),((6,"flex"),.5421)])
def test_breakevens(st, be): assert breakeven(st) == pytest.approx(be, abs=6e-4)

@pytest.mark.parametrize("st,ev", [((5,"flex"),.269),((6,"flex"),.402),((6,"power"),.428),((3,"power"),.171)])
def test_ev_at_58(st, ev): assert entry_ev([0.58] * st[0], st) == pytest.approx(ev, abs=2e-3)

def test_kelly_5flex():
    f, g = kelly_entry([0.58] * 5, (5, "flex"))
    assert f == pytest.approx(0.0664, abs=2e-3) and g == pytest.approx(0.0078, abs=5e-4)

def test_hit_distribution_sums(): assert sum(hit_distribution([.5, .6, .7])) == pytest.approx(1)

def test_builder_enforces_constraints(settings):
    picks = [Pick(f"P{i}", "KC" if i < 6 else "BUF", f"g{i}", "rush_yds", 50.5, "over", 0.60, True) for i in range(8)]
    legs, st, ev, g = build_best_entry(picks, settings)
    games = [p.game_id for p in legs]
    assert st in ALLOWED and len({p.team for p in legs}) >= 2 and ev > 0
    assert max(games.count(x) for x in games) <= settings.pp_max_per_game

def test_builder_none_when_no_edge(settings):
    assert build_best_entry([Pick(f"P{i}", f"T{i}", f"g{i}", "x", 1.5, "over", 0.53, True) for i in range(6)], settings) is None

def test_builder_one_team_only_none(settings):
    assert build_best_entry([Pick(f"P{i}", "KC", f"g{i}", "x", 1.5, "over", 0.65, True) for i in range(6)], settings) is None

@pytest.mark.parametrize("st,dnp,tie,teams,exp", [((6,"flex"),1,0,2,(5,"flex")), ((3,"flex"),1,0,2,(2,"power")),
    ((2,"power"),1,0,2,None), ((2,"flex"),1,0,2,None), ((4,"power"),1,0,1,None), ((4,"power"),0,1,1,(3,"power"))])
def test_revert(st, dnp, tie, teams, exp): assert revert(st, dnp, tie, teams) == exp


# -- quant upgrade: payout mode, settings tables, greedy selector ------------------------------
from dataclasses import replace as _replace  # noqa: E402

from gridiron_edge.strategy.prizepicks import PAYOUTS, allowed_structures, payout_table  # noqa: E402


def test_payout_table_fixed_and_arena(settings):
    assert payout_table(settings) == PAYOUTS and allowed_structures(settings) == ALLOWED
    arena = payout_table(_replace(settings, pp_mode="arena"))
    assert arena[(5, "flex")] == {5: 10} and arena[(5, "power")] == {5: 20}
    # 5-Flex at 58% per pick: +26.9% on the fixed table, about -34% when only a perfect card pays
    assert entry_ev([0.58] * 5, (5, "flex"), arena) == pytest.approx(-0.344, abs=2e-3)
    assert entry_ev([0.58] * 5, (5, "power"), arena) == pytest.approx(0.312, abs=2e-3)


def test_payout_override_and_structures(settings):
    s = _replace(settings, pp_payouts_json='{"5-flex": {"5": 9, "4": 2}}', pp_structures=("5-flex", "3-power"))
    assert payout_table(s)[(5, "flex")] == {5: 9, 4: 2} and payout_table(s)[(3, "power")] == {3: 6}
    assert allowed_structures(s) == [(5, "flex"), (3, "power")]


def test_arena_builder_avoids_flex(settings):
    s = _replace(settings, pp_mode="arena")
    picks = [Pick(f"P{i}", "KC" if i % 2 else "BUF", f"g{i}", "x", 1.5, "over", 0.60, True) for i in range(8)]
    legs, st, ev, g = build_best_entry(picks, s, allowed_structures(s), payouts=payout_table(s))
    assert st[1] == "power" and ev > 0


def test_greedy_matches_brute_force(settings):
    """Exact under independent picks (copula_rho 0): the greedy candidates hold the optimum."""
    import random
    settings = _replace(settings, copula_rho=0.0)
    rng = random.Random(7)
    for trial in range(40):
        picks = []
        for i in range(rng.randint(4, 11)):
            g = f"g{rng.randint(0, 6)}"
            team = rng.choice(["KC", "BUF"]) if trial % 3 else "KC" if i else "BUF"
            picks.append(Pick(f"P{i}", team, g, "x", 1.5, "over", rng.uniform(0.52, 0.68), rng.random() < 0.7))
        brute = build_best_entry(picks, settings, brute=True)
        greedy = build_best_entry(picks, settings)
        if brute is None:
            assert greedy is None
            continue
        assert greedy is not None and greedy[1] == brute[1]
        assert greedy[3] == pytest.approx(brute[3], abs=1e-9) and greedy[2] == pytest.approx(brute[2], abs=1e-9)


# -- same-game picks: up to pp_max_per_game, scored robustly over the correlation ---------------
from gridiron_edge.strategy.prizepicks import (  # noqa: E402
    corr_sign, correlated_hit_distribution, mvn_cdf, robust_ev, robust_kelly,
)


def _pk(name, team, game, p, side="over"):
    return Pick(name, team, game, "rush_yds", 50.5, side, p, True)


def test_two_same_game_picks_allowed(settings):
    # only two games have lines: a 3-pick Power needs two picks from one of them
    picks = [_pk("A", "KC", "g1", 0.66), _pk("B", "BUF", "g1", 0.65), _pk("C", "MIA", "g2", 0.64)]
    legs, st, ev, g = build_best_entry(picks, settings)
    assert st == (3, "power") and {p.player for p in legs} == {"A", "B", "C"} and ev > 0


def test_three_same_game_picks_not_allowed_by_default(settings):
    picks = [_pk("A", "KC", "g1", 0.70), _pk("B", "BUF", "g1", 0.70), _pk("C", "KC", "g1", 0.70)]
    assert settings.pp_max_per_game == 2
    assert build_best_entry(picks, settings) is None
    assert build_best_entry(picks, settings, brute=True) is None
    legs, st, _, _ = build_best_entry(picks, _replace(settings, pp_max_per_game=3))
    assert st == (3, "power") and len(legs) == 3


def test_same_game_entry_still_needs_two_teams(settings):
    picks = [_pk("A", "KC", "g1", 0.70), _pk("B", "KC", "g1", 0.70), _pk("C", "KC", "g2", 0.70)]
    assert build_best_entry(picks, settings) is None
    assert build_best_entry(picks, settings, brute=True) is None


def test_max_per_game_setting_validated():
    from gridiron_edge.config import Settings, apply_override
    assert apply_override(Settings(), "pp_max_per_game", "3").pp_max_per_game == 3
    for bad in ("0", "4"):
        with pytest.raises(ValueError):
            apply_override(Settings(), "pp_max_per_game", bad)


def test_corr_sign():
    qb, wr = _pk("QB", "KC", "g", 0.6), _pk("WR", "KC", "g", 0.6)
    assert corr_sign(qb, wr) == 1  # same team, same direction (a QB and his own receiver)
    assert corr_sign(qb, _pk("WR", "KC", "g", 0.6, "under")) == -1  # opposite directions
    assert corr_sign(qb, _pk("X", "BUF", "g", 0.6, "under")) == -1
    assert corr_sign(qb, _pk("X", "BUF", "g", 0.6)) == 0
    assert corr_sign(qb, _pk("X", "KC", "g2", 0.6)) == 0  # different games are independent


def test_mvn_cdf_matches_scipy():
    from scipy.stats import multivariate_normal
    a = [0.3, -0.2, 0.5]
    for r in ([[1, .3, 0], [.3, 1, -.3], [0, -.3, 1]], [[1, -.3, -.3], [-.3, 1, .3], [-.3, .3, 1]]):
        ref = multivariate_normal(mean=[0, 0, 0], cov=r).cdf(a, rng=1)
        assert mvn_cdf(a, r) == pytest.approx(ref, abs=2e-4)
        ref2 = multivariate_normal(mean=[0, 0], cov=[[1, r[0][1]], [r[0][1], 1]]).cdf(a[:2], rng=1)
        assert mvn_cdf(a[:2], [row[:2] for row in r[:2]]) == pytest.approx(ref2, abs=2e-4)


def test_correlated_distribution_exact_and_deterministic():
    legs = [_pk("A", "KC", "g", 0.6), _pk("B", "KC", "g", 0.55), _pk("C", "BUF", "h", 0.58)]
    assert correlated_hit_distribution(legs, 0.0) == pytest.approx(hit_distribution([0.6, 0.55, 0.58]))
    pos = correlated_hit_distribution(legs, 0.3)
    assert sum(pos) == pytest.approx(1) and pos == correlated_hit_distribution(legs, 0.3)
    # positive correlation fattens both tails, keeps the mean
    ind = hit_distribution([0.6, 0.55, 0.58])
    assert pos[3] > ind[3] and pos[0] > ind[0]
    assert sum(k * q for k, q in enumerate(pos)) == pytest.approx(0.6 + 0.55 + 0.58, abs=1e-6)


def test_robust_ev_not_above_independent():
    legs = [_pk("A", "KC", "g", 0.62), _pk("B", "KC", "g", 0.60), _pk("C", "BUF", "h", 0.61),
            _pk("D", "MIA", "i", 0.6), _pk("E", "NYJ", "j", 0.6)]
    for st in ((5, "flex"), (5, "power")):
        ind = entry_ev([p.p for p in legs], st)
        assert robust_ev(legs, st, PAYOUTS, 0.3) <= ind + 1e-12
        assert robust_ev(legs, st, PAYOUTS, 0.0) == pytest.approx(ind)
        assert robust_kelly(legs, st, PAYOUTS, 0.3)[1] <= kelly_entry([p.p for p in legs], st)[1] + 1e-9
    # a 5-pick Flex is hurt by fatter tails (it pays on 3 and 4 hits): strictly below
    assert robust_ev(legs, (5, "flex"), PAYOUTS, 0.3) < entry_ev([p.p for p in legs], (5, "flex")) - 1e-4
    # cross-game picks only: no correlation to be unsure about
    solo = [_pk(c, t, f"g{i}", 0.6) for i, (c, t) in enumerate(zip("ABCDE", ["KC", "BUF"] * 3))]
    assert robust_ev(solo, (5, "flex"), PAYOUTS, 0.3) == pytest.approx(entry_ev([0.6] * 5, (5, "flex")))


def test_greedy_never_beats_brute_with_correlation(settings):
    import random
    rng = random.Random(11)
    for trial in range(15):
        picks = [Pick(f"P{i}", rng.choice(["KC", "BUF", "MIA"]), f"g{rng.randint(0, 3)}", "x", 1.5,
                      rng.choice(["over", "under"]), rng.uniform(0.55, 0.68), True) for i in range(rng.randint(5, 9))]
        brute = build_best_entry(picks, settings, brute=True)
        greedy = build_best_entry(picks, settings)
        if greedy is None:
            continue
        legs = greedy[0]
        assert len({p.team for p in legs}) >= 2 and len({p.player for p in legs}) == len(legs)
        assert brute is not None and greedy[3] <= brute[3] + 1e-9
