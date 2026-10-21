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
KERNEL_BANDWIDTH = 1.0  # points of spread
KERNEL_MIN_ESS = 150  # widen the kernel until this many effective games carry the weight
KERNEL_MAX_BANDWIDTH = 8.0


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
            # round half away from zero: np.rint's banker's rounding inflates even residuals
            values, counts = np.unique(_round_half_away(res), return_counts=True)
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


def _round_half_away(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    return (np.sign(x) * np.floor(np.abs(x) + 0.5)).astype(int)


class ConditionalMarginModel:
    """Empirical integer margin conditional on the closing spread.

    `mean` is the expected margin (the spread). Each game enters as (s, m) and (-s, -m), so the
    fit is oriented to the favourite and symmetric. Games are weighted by a Gaussian kernel on
    s - mean; margins stay integers and the mean is never rounded, so push and key-number mass
    sit where the closing spread puts them.
    """

    def __init__(self, spreads, margins, sigma_fallback: float, bandwidth: float = KERNEL_BANDWIDTH,
                 min_ess: float = KERNEL_MIN_ESS):
        s, m = np.asarray(spreads, dtype=float), np.asarray(margins, dtype=float)
        ok = np.isfinite(s) & np.isfinite(m)
        s, m = s[ok], _round_half_away(m[ok])
        self._normal = NormalModel(sigma_fallback)
        self.n = len(s)
        self._use_normal = self.n < MIN_EMPIRICAL_RESIDUALS
        self._s = np.concatenate([s, -s])
        self._m = np.concatenate([m, -m])
        self.bandwidth, self.min_ess = bandwidth, min_ess
        self._cache: dict[float, tuple[np.ndarray, int]] = {}

    def _weights(self, mean: float) -> tuple[np.ndarray, int]:
        """Normalised kernel weights and an integer margin shift (non-zero only past the data)."""
        key = round(float(mean), 6)
        if (hit := self._cache.get(key)) is not None:
            return hit
        bw = self.bandwidth
        while True:
            w = np.exp(-0.5 * ((self._s - mean) / bw) ** 2)
            tot, sq = float(w.sum()), float((w * w).sum())
            if bw >= KERNEL_MAX_BANDWIDTH or (sq > 0 and tot * tot / sq >= self.min_ess):
                break
            bw *= 1.5
        if tot <= 1e-12:  # far outside the data: centre the nearest games on the mean
            w = (np.abs(self._s - mean) == np.abs(self._s - mean).min()).astype(float)
            tot = float(w.sum())
        w = w / tot
        gap = mean - float(w @ self._s)
        shift = int(_round_half_away(np.array([gap]))[0]) if abs(gap) >= 1 else 0
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = (w, shift)
        return w, shift

    def p_greater(self, mean: float, x: float) -> float:
        if self._use_normal:
            return self._normal.p_greater(mean, x)
        w, shift = self._weights(mean)
        return float(w[self._m + shift > x].sum())

    def p_equal(self, mean: float, x: float) -> float:
        if self._use_normal or float(x) != math.floor(x):
            return 0.0
        w, shift = self._weights(mean)
        return float(w[self._m + shift == int(x)].sum())


def p_push(model: MarginModel, mean: float, x: float) -> float:
    """P(value == x). Nonzero only at integer x for the empirical models."""
    p_equal = getattr(model, "p_equal", None)
    return float(p_equal(mean, x)) if p_equal is not None else 0.0


def p_cover(model: MarginModel, mean: float, x: float) -> float:
    """P(value > x | value != x): what a devigged two-way price at line x measures."""
    rest = 1.0 - p_push(model, mean, x)
    return model.p_greater(mean, x) / rest if rest > 1e-9 else 0.5


def implied_mean(model: MarginModel, x: float, p: float, width: float = 80.0) -> float | None:
    """Mean whose push-aware P(value > x | no push) equals p, or None when no mean reproduces it."""
    try:
        return float(brentq(lambda mu: p_cover(model, mu, x) - p, x - width, x + width, xtol=1e-4))
    except ValueError:
        return None


# Dispersion (sd / mean) of yardage around a sharp book line L. Yardage is right-skewed, so a gamma with
# its median set by the line is used. The spread falls with the median: a 10-yard tight end catches
# nothing in ~20% of games yet clears 3x his line in ~13%, while a 75-yard WR1 rarely doubles it.
# CV(L) = a * (max(L, PROP_CV_FLOOR) / ref) ** b, capped at PROP_CV_MAX (a CV above 1 puts the gamma's
# mode at 0, which is the zero-catch game). Fitted by models.props.fit_prop_cv on nflverse 2024-25
# player-games (week 5 on) and checked against Kalshi's own yardage ladders; frozen here.
PROP_CV_FIT = {"reception_yds": (0.835, 20.0, -0.41), "rush_yds": (0.67, 40.0, -0.5)}
PROP_CV_FIXED = {"pass_yds": 0.30}
PROP_CV_FLOOR = 3.0
PROP_CV_MAX = 1.6
DEFAULT_YARDS_CV = 0.50


def prop_cv(stat: str | None, line: float) -> float:
    """sd / mean of the stat for a player whose median (book line) is `line`."""
    if stat in PROP_CV_FIXED:
        return PROP_CV_FIXED[stat]
    fit = PROP_CV_FIT.get(stat or "")
    if fit is None:
        return DEFAULT_YARDS_CV
    a, ref, b = fit
    return min(a * (max(line, PROP_CV_FLOOR) / ref) ** b, PROP_CV_MAX)


def prop_p_over(
    line: float,
    mean: float,
    kind: Literal["yards", "count"],
    sd: float | None = None,
    stat: str | None = None,
    anchor: float | None = None,
) -> float:
    """P(value > line). Yards: gamma (sd given, else the stat's CV at the anchor book line, which
    defaults to `line`). Counts: Poisson, or negative binomial when sd shows overdispersion; an integer
    line's push is not an over."""
    if kind == "yards":
        sd = prop_cv(stat, line if anchor is None else anchor) * mean if sd is None else sd
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
