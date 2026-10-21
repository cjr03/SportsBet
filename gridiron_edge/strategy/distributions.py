"""Margin and prop outcome distributions."""

import math
from typing import Literal, Protocol

import numpy as np
from scipy.optimize import brentq
from scipy.stats import gamma, nbinom, norm, poisson

NFL_ML_SIGMA = 11.16
NFL_SPREAD_SIGMA = 13.0
NFL_TOTAL_SIGMA = 13.0
CFB_SIGMA = 15.75

MIN_EMPIRICAL_RESIDUALS = 500


class MarginModel(Protocol):
    def p_greater(self, mean: float, x: float) -> float:
        """P(value > x) when the expected value is `mean`."""
        ...


class NormalModel:
    def __init__(self, sigma: float):
        self.sigma = sigma

    def p_greater(self, mean: float, x: float) -> float:
        return float(norm.sf(x, loc=mean, scale=self.sigma))


class EmpiricalMarginModel:
    """Discrete distribution over integer residuals, so key-number mass survives."""

    def __init__(self, residuals: np.ndarray, sigma_fallback: float):
        self._normal = NormalModel(sigma_fallback)
        res = np.asarray(residuals)
        self._use_normal = len(res) < MIN_EMPIRICAL_RESIDUALS
        if not self._use_normal:
            values, counts = np.unique(np.rint(res).astype(int), return_counts=True)
            self._values = values
            self._probs = counts / counts.sum()

    @staticmethod
    def _shift(mean: float) -> float:
        return round(mean * 2) / 2

    def p_greater(self, mean: float, x: float) -> float:
        if self._use_normal:
            return self._normal.p_greater(mean, x)
        return float(self._probs[self._values + self._shift(mean) > x].sum())

    def p_equal(self, mean: float, x: float) -> float:
        if self._use_normal:
            return 0.0
        return float(self._probs[np.isclose(self._values + self._shift(mean), x)].sum())


def p_push(model: MarginModel, mean: float, x: float) -> float:
    """P(value == x). Nonzero only at integer x for the empirical model."""
    if isinstance(model, EmpiricalMarginModel):
        return model.p_equal(mean, x)
    return 0.0


# Dispersion (sd / mean) of yardage around a sharp book line; yardage is right-skewed, so a gamma with
# its median set by the line replaces the old Normal(sd = 0.6*mean + 5). Priors: tune by backtest.
PROP_CV = {"reception_yds": 0.55, "rush_yds": 0.50, "pass_yds": 0.30}
DEFAULT_YARDS_CV = 0.50


def prop_p_over(
    line: float,
    mean: float,
    kind: Literal["yards", "count"],
    sd: float | None = None,
    stat: str | None = None,
) -> float:
    """P(value > line). Yards: gamma (sd given, else the stat's CV). Counts: Poisson, or negative
    binomial when sd shows overdispersion; an integer line's push is not an over."""
    if kind == "yards":
        sd = PROP_CV.get(stat or "", DEFAULT_YARDS_CV) * mean if sd is None else sd
        k = (mean / sd) ** 2
        return float(gamma.sf(line, k, scale=mean / k))
    x = math.floor(line)
    if sd is not None and sd * sd > mean > 0:
        r = mean * mean / (sd * sd - mean)
        return float(nbinom.sf(x, r, r / (r + mean)))
    return float(poisson.sf(x, mean))


def implied_mean_from_line(
    line: float,
    p_over: float,
    kind: Literal["yards", "count"],
    sd: float | None = None,
    stat: str | None = None,
) -> float:
    """Mean whose model P(over line) equals p_over."""
    return float(brentq(lambda m: prop_p_over(line, m, kind, sd, stat) - p_over, 1e-6, 5000.0))
