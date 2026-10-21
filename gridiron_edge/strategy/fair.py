"""Sharp-book fair probability and implied means.

Tiers, best first: Pinnacle; the exchanges (novig/prophetx, hold-capped); mid-tier low-vig books;
the last sharp price anchored to how far the soft-book consensus moved since; the soft-book median.
Betting venues (Kalshi, Polymarket) are never part of their own reference price.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from gridiron_edge.strategy.devig import devig
from gridiron_edge.strategy.distributions import (
    NFL_SPREAD_SIGMA, NFL_TOTAL_SIGMA, MarginModel, NormalModel, implied_mean,
)
from gridiron_edge.types import Quote

NON_REFERENCE = frozenset({"kalshi", "polymarket"})  # venues we bet on: never their own fair price
PINNACLE = ("pinnacle",)
EXCHANGES = ("novig", "prophetx")
MID_TIER = ("lowvig", "betonlineag")  # low-vig, but not Pinnacle-grade
TIERS = (("pinnacle", PINNACLE), ("exchanges", EXCHANGES), ("mid", MID_TIER))
TIER_RANK = {name: i for i, name in enumerate(
    [t for t, _ in TIERS] + [f"anchored-{t}" for t, _ in TIERS] + ["median"])}
SHARP_BOOKS = frozenset(PINNACLE + EXCHANGES + MID_TIER)
EXCHANGE_MAX_HOLD = 0.045  # a thin exchange book with a wide spread is not a sharp price
MIN_HOLD_WEIGHT = 0.005  # 1/hold weights: floor the hold so a zero-hold book can't take all the weight
ANCHOR_MAX_SECONDS = 3 * 86400
ANCHOR_MAX_MOVE = 0.015  # consensus moved more than this since the sharp pull: the anchor is void
KEY_NUMBERS = (3, 7, 10)
SAME_PULL = timedelta(seconds=120)  # quotes fetched this close together came from one pull
_SIDE_A = {"h2h": "home", "spreads": "home", "totals": "over"}
_SIDE_B = {"h2h": "away", "spreads": "away", "totals": "under"}
_METHOD = {"h2h": "power", "spreads": "additive", "totals": "additive"}


@dataclass(frozen=True)
class BookPrice:
    p: float  # no-vig P(side A)
    hold: float  # overround of the pair
    at: datetime  # when we fetched it


@dataclass(frozen=True)
class FairPrice:
    p: float  # no-vig P(side A: home/over)
    source: str  # pinnacle | exchanges | mid | anchored-<tier> | median
    books: tuple[str, ...]

    @property
    def tier(self) -> int:
        """0 = best. Used to prefer a sharper source over a point nearer even money."""
        return TIER_RANK.get(self.source, len(TIER_RANK))


def _book_probs(
    quotes: Sequence[Quote], market: str, point: float | None, now: datetime, stale_seconds: float
) -> dict[str, BookPrice]:
    """No-vig side-A probability per reference book with both sides fetched within stale_seconds.

    Both sides of a pair come from the same source; a book seen through two sources keeps the
    more recent pull.
    """
    a_side, b_side = _SIDE_A[market], _SIDE_B[market]
    # spreads: the away side carries the opposite point
    b_point = -point if (market == "spreads" and point is not None) else point
    best: dict[tuple[str, str, str], Quote] = {}
    for qt in quotes:
        if qt.market != market or qt.book in NON_REFERENCE:
            continue
        if (now - qt.seen_at).total_seconds() > stale_seconds:
            continue
        if qt.side == a_side and qt.point == point:
            key = (qt.book, qt.source, "a")
        elif qt.side == b_side and qt.point == b_point:
            key = (qt.book, qt.source, "b")
        else:
            continue
        if key not in best or qt.seen_at > best[key].seen_at:
            best[key] = qt
    out: dict[str, BookPrice] = {}
    for book, source, _ in sorted(best):
        a, b = best.get((book, source, "a")), best.get((book, source, "b"))
        if a is None or b is None:
            continue
        at = min(a.seen_at, b.seen_at)
        if book in out and out[book].at >= at:
            continue
        p = devig([a.decimal, b.decimal], _METHOD[market])[0]
        out[book] = BookPrice(p, 1 / a.decimal + 1 / b.decimal - 1, at)
    return out


def _tier_price(probs: dict[str, BookPrice]) -> tuple[str, float, tuple[str, ...]] | None:
    """Best sharp tier present: (tier, 1/hold-weighted p, books)."""
    for name, books in TIERS:
        sel = {b: probs[b] for b in books if b in probs
               and (name != "exchanges" or probs[b].hold <= EXCHANGE_MAX_HOLD)}
        if sel:
            w = {b: 1 / max(bp.hold, MIN_HOLD_WEIGHT) for b, bp in sel.items()}
            p = sum(w[b] * sel[b].p for b in sel) / sum(w.values())
            return name, p, tuple(sorted(sel))
    return None


def _soft(probs: dict[str, BookPrice]) -> dict[str, BookPrice]:
    return {b: bp for b, bp in probs.items() if b not in SHARP_BOOKS}


def _main_spread(quotes: Sequence[Quote], now: datetime, age: float) -> float | None:
    """Soft-book consensus home spread: the point whose median no-vig P is nearest 0.5."""
    best: tuple[float, float] | None = None
    for pt in {q.point for q in quotes if q.market == "spreads" and q.side == "home" and q.point is not None}:
        soft = _soft(_book_probs(quotes, "spreads", pt, now, age))
        if soft:
            d = abs(statistics.median(bp.p for bp in soft.values()) - 0.5)
            if best is None or d < best[0]:
                best = (d, pt)
    return None if best is None else best[1]


def _crossed_key(a: float, b: float) -> bool:
    lo, hi = sorted((a, b))
    return any(lo < k < hi for key in KEY_NUMBERS for k in (key, -key))


def _anchored(quotes: Sequence[Quote], market: str, point: float | None, now: datetime,
              fresh: dict[str, BookPrice], max_age: float) -> FairPrice | None:
    """The last sharp no-vig price, shifted by the soft-book consensus move since that pull.

    None when there is nothing to compare against, the consensus moved more than ANCHOR_MAX_MOVE,
    or (spreads and moneylines) the consensus spread crossed 3, 7 or 10.
    """
    old = _book_probs(quotes, market, point, now, max_age)
    got = _tier_price({b: bp for b, bp in old.items() if b in SHARP_BOOKS})
    if got is None:
        return None
    tier, _, books = got
    pulled = max(old[b].at for b in books)
    # the sharp price and the soft books from that same pull
    then_quotes = [q for q in quotes if abs(q.seen_at - pulled) <= SAME_PULL]
    then = _book_probs(then_quotes, market, point, now, max_age)
    got = _tier_price({b: bp for b, bp in then.items() if b in SHARP_BOOKS})
    if got is None:
        return None
    _, p_sharp, books = got
    soft_then, soft_now = _soft(then), _soft(fresh)
    common = soft_then.keys() & soft_now.keys()
    if common:
        soft_then = {b: soft_then[b] for b in common}
        soft_now = {b: soft_now[b] for b in common}
    if not soft_then or not soft_now:
        return None
    move = (statistics.median(bp.p for bp in soft_now.values())
            - statistics.median(bp.p for bp in soft_then.values()))
    if abs(move) > ANCHOR_MAX_MOVE:
        return None
    if market in ("spreads", "h2h"):
        was = _main_spread(then_quotes, now, max_age)
        fresh_age = (now - min(bp.at for bp in soft_now.values())).total_seconds()
        is_ = _main_spread([q for q in quotes if q.book not in SHARP_BOOKS], now, fresh_age)
        if was is not None and is_ is not None and _crossed_key(was, is_):
            return None
    p = min(max(p_sharp + move, 0.001), 0.999)
    return FairPrice(p, f"anchored-{tier}", tuple(sorted(set(books) | soft_now.keys())))


def fair_price(
    quotes: Sequence[Quote],
    market: str,
    point: float | None,
    *,
    now: datetime,
    stale_seconds: float = 600,
    anchor_max_seconds: float = ANCHOR_MAX_SECONDS,
) -> FairPrice | None:
    """No-vig P(side A) from the best available tier, with the books behind it."""
    fresh = _book_probs(quotes, market, point, now, stale_seconds)
    got = _tier_price(fresh)
    if got is not None:
        tier, p, books = got
        return FairPrice(p, tier, books)
    if anchor_max_seconds > stale_seconds:
        anchored = _anchored(quotes, market, point, now, fresh, anchor_max_seconds)
        if anchored is not None:
            return anchored
    if not fresh:
        return None
    return FairPrice(statistics.median(bp.p for bp in fresh.values()), "median", tuple(sorted(fresh)))


def fair_two_way(
    quotes: Sequence[Quote],
    market: str,
    point: float | None,
    *,
    now: datetime,
    stale_seconds: float = 600,
    anchor_max_seconds: float = ANCHOR_MAX_SECONDS,
) -> tuple[float, str] | None:
    """No-vig P(side A: home/over) and its source label."""
    fp = fair_price(quotes, market, point, now=now, stale_seconds=stale_seconds,
                    anchor_max_seconds=anchor_max_seconds)
    return None if fp is None else (fp.p, fp.source)


@dataclass(frozen=True)
class ImpliedMean:
    mean: float
    point: float  # the quoted point it was inverted from
    fair: FairPrice


def implied_market_mean(
    quotes: Sequence[Quote],
    market: str,
    model: MarginModel,
    *,
    now: datetime,
    stale_seconds: float = 600,
    anchor_max_seconds: float = ANCHOR_MAX_SECONDS,
) -> ImpliedMean | None:
    """Expected home margin (spreads) or total (totals) implied by the best-sourced quoted point.

    Points from a sharper tier win over points nearer even money. The devigged price at an integer
    line is P(cover | no push), so the inversion is push-aware on the same model that later prices
    the Kalshi line.
    """
    points = {
        qt.point
        for qt in quotes
        if qt.market == market and qt.side == _SIDE_A[market] and qt.point is not None
    }
    best: tuple[int, float, float, FairPrice] | None = None  # (tier, distance from 0.5, point, fair)
    for pt in sorted(points):
        fp = fair_price(quotes, market, pt, now=now, stale_seconds=stale_seconds,
                        anchor_max_seconds=anchor_max_seconds)
        if fp is None:
            continue
        cand = (fp.tier, abs(fp.p - 0.5), pt, fp)
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        return None
    _, _, pt, fp = best
    # spreads: the home side at point pt covers when the home margin beats -pt
    line = -pt if market == "spreads" else pt
    mean = implied_mean(model, line, fp.p)
    return None if mean is None else ImpliedMean(mean, pt, fp)


def fair_spread_mean(quotes: Sequence[Quote], *, now: datetime, model: MarginModel | None = None,
                     stale_seconds: float = 600) -> float | None:
    """Expected home margin implied by the sharp spread."""
    got = implied_market_mean(quotes, "spreads", model or NormalModel(NFL_SPREAD_SIGMA), now=now,
                              stale_seconds=stale_seconds)
    return None if got is None else got.mean


def fair_total_mean(quotes: Sequence[Quote], *, now: datetime, model: MarginModel | None = None,
                    stale_seconds: float = 600) -> float | None:
    """Expected game total implied by the sharp total."""
    got = implied_market_mean(quotes, "totals", model or NormalModel(NFL_TOTAL_SIGMA), now=now,
                              stale_seconds=stale_seconds)
    return None if got is None else got.mean
