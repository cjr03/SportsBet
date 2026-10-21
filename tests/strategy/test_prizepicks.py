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
    assert st in ALLOWED and len({p.team for p in legs}) >= 2 and len({p.game_id for p in legs}) == len(legs) and ev > 0

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
    import random
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
