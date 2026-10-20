"""Sharp-book fair probability and implied means."""

import statistics
from collections.abc import Sequence
from datetime import datetime

from gridiron_edge.strategy.devig import devig
from gridiron_edge.strategy.distributions import NFL_SPREAD_SIGMA, NFL_TOTAL_SIGMA
from gridiron_edge.types import Quote

EXCHANGES = ("novig", "prophetx")
_SIDE_A = {"h2h": "home", "spreads": "home", "totals": "over"}
_SIDE_B = {"h2h": "away", "spreads": "away", "totals": "under"}
_METHOD = {"h2h": "power", "spreads": "additive", "totals": "additive"}
_SQRT_HALF_PI = 1.2533  # sqrt(pi/2): slope of the normal quantile near 0.5 is 1/(sigma*0.3989)


def _book_probs(
    quotes: Sequence[Quote], market: str, point: float | None, now: datetime, stale_seconds: float
) -> dict[str, float]:
    """No-vig side-A probability per book with both sides fresh at this point."""
    a_side, b_side = _SIDE_A[market], _SIDE_B[market]
    # spreads: the away side carries the opposite point
    b_point = -point if (market == "spreads" and point is not None) else point
    best: dict[tuple[str, str], Quote] = {}
    for qt in quotes:
        if qt.market != market or (now - qt.last_update).total_seconds() > stale_seconds:
            continue
        if qt.side == a_side and qt.point == point:
            key = (qt.book, "a")
        elif qt.side == b_side and qt.point == b_point:
            key = (qt.book, "b")
        else:
            continue
        if key not in best or qt.last_update > best[key].last_update:
            best[key] = qt
    out: dict[str, float] = {}
    for book in {b for b, _ in best}:
        if (book, "a") in best and (book, "b") in best:
            out[book] = devig(
                [best[(book, "a")].decimal, best[(book, "b")].decimal], _METHOD[market]
            )[0]
    return out


def fair_two_way(
    quotes: Sequence[Quote],
    market: str,
    point: float | None,
    *,
    now: datetime,
    stale_seconds: float = 600,
) -> tuple[float, str] | None:
    """No-vig P(side A: home/over) and its source label."""
    probs = _book_probs(quotes, market, point, now, stale_seconds)
    if not probs:
        return None
    if "pinnacle" in probs:
        return probs["pinnacle"], "pinnacle"
    exch = [probs[b] for b in EXCHANGES if b in probs]
    if exch:
        return sum(exch) / len(exch), "exchanges"
    return statistics.median(probs.values()), "median"


def _implied_mean(quotes: Sequence[Quote], market: str, sigma: float, now: datetime) -> float | None:
    points = {
        qt.point
        for qt in quotes
        if qt.market == market and qt.side == _SIDE_A[market] and qt.point is not None
    }
    best: tuple[float, float, float] | None = None  # (distance from 0.5, point, p)
    for pt in sorted(points):
        fair = fair_two_way(quotes, market, pt, now=now)
        if fair is None:
            continue
        dist = abs(fair[0] - 0.5)
        if best is None or dist < best[0]:
            best = (dist, pt, fair[0])
    if best is None:
        return None
    _, pt, p = best
    base = -pt if market == "spreads" else pt
    # P(A) above 0.5 means the expected value sits above the base line
    return base + (p - 0.5) * 2 * sigma * _SQRT_HALF_PI


def fair_spread_mean(quotes: Sequence[Quote], *, now: datetime) -> float | None:
    """Expected home margin implied by the sharp spread."""
    return _implied_mean(quotes, "spreads", NFL_SPREAD_SIGMA, now)


def fair_total_mean(quotes: Sequence[Quote], *, now: datetime) -> float | None:
    """Expected game total implied by the sharp total."""
    return _implied_mean(quotes, "totals", NFL_TOTAL_SIGMA, now)
