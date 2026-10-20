"""Remove the bookmaker margin from a set of decimal odds."""

import math
from collections.abc import Sequence

from scipy.optimize import brentq

from gridiron_edge.strategy.odds import implied

METHODS = ("multiplicative", "additive", "power", "shin")


def devig(decimals: Sequence[float], method: str) -> list[float]:
    r = [implied(d) for d in decimals]
    total = sum(r)
    n = len(r)
    if method == "multiplicative":
        return [x / total for x in r]
    if method == "additive":
        return [x - (total - 1) / n for x in r]
    if method == "power":
        k = brentq(lambda k: sum(x**k for x in r) - 1, 0.2, 5)
        return [x**k for x in r]
    if method == "shin":
        if total <= 1:  # no margin to remove; the root is z = 0
            return [x / total for x in r]
        def probs(z: float) -> list[float]:
            return [
                (math.sqrt(z * z + 4 * (1 - z) * x * x / total) - z) / (2 * (1 - z))
                for x in r
            ]

        z = brentq(lambda z: sum(probs(z)) - 1, 1e-9, 0.5 - 1e-9)
        return probs(z)
    raise ValueError(f"unknown devig method: {method}")


def devig_worst(decimals: Sequence[float], side_index: int) -> float:
    """Most conservative fair probability for one side across all methods."""
    return min(devig(decimals, m)[side_index] for m in METHODS)
