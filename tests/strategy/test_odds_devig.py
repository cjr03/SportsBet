import pytest

from gridiron_edge.strategy.devig import devig, devig_worst
from gridiron_edge.strategy.odds import american_to_decimal, decimal_to_american


def test_american_decimal():
    assert american_to_decimal(-110) == pytest.approx(1.909090, 1e-5)
    assert american_to_decimal(150) == 2.5 and decimal_to_american(2.5) == 150


@pytest.mark.parametrize("m", ["multiplicative", "additive", "power", "shin"])
def test_symmetric_is_half(m):
    assert devig([1.9091, 1.9091], m) == pytest.approx([0.5, 0.5], abs=1e-6)


def test_additive_equals_shin_two_way():
    assert devig([1.80, 2.10], "additive")[0] == pytest.approx(0.53968, abs=1e-5)
    assert devig([1.80, 2.10], "shin")[0] == pytest.approx(0.53968, abs=1e-5)


def test_power_sums_to_one_and_matches_oracle():
    import penaltyblog as pb

    p = devig([1.40, 3.20], "power")
    assert sum(p) == pytest.approx(1)
    oracle = pb.implied.calculate_implied([1.40, 3.20], method="power").probabilities
    assert p == pytest.approx(list(oracle), abs=1e-6)


def test_worst_case():
    assert devig_worst([1.80, 2.10], 0) == min(
        devig([1.80, 2.10], m)[0] for m in ("multiplicative", "additive", "power", "shin")
    )


def test_shin_no_margin():
    assert devig([2.0, 2.0], "shin") == pytest.approx([0.5, 0.5])
