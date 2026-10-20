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
