"""Arbitrage detection: Kalshi ML pairs, spread/total ladders, cross-venue."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from gridiron_edge.strategy.kalshi_pricing import effective_price
from gridiron_edge.types import KalshiMarket, Leg


@dataclass
class Arb:
    kind: Literal["kalshi_ml_pair", "kalshi_ladder", "cross_venue"]
    game_id: str | None
    legs: list[Leg]
    qty: int
    cost: float
    payout: float
    roi: float
    tie_risk: bool


def _leg(m: KalshiMarket, side: str, price: float) -> Leg:
    return Leg("kalshi", f"{m.ticker} {side.upper()}", side, price,
               price, m.game_id, ticker=m.ticker, line=m.line, team=m.team)


def kalshi_ml_pairs(markets: list[KalshiMarket], league_of: Callable[[KalshiMarket], str]) -> list[Arb]:
    groups: dict[str, list[KalshiMarket]] = defaultdict(list)
    for m in markets:
        if m.market_type == "ml" and m.status == "active":
            groups[m.event_ticker].append(m)
    out: list[Arb] = []
    for ms in groups.values():
        if len(ms) != 2:
            continue
        qty = int(min(m.yes_ask_size for m in ms))
        if qty < 1:
            continue
        cost = sum(effective_price(m.yes_ask, qty, m.fee_multiplier) * qty for m in ms)
        roi = qty / cost - 1
        if roi > 0:
            out.append(Arb("kalshi_ml_pair", ms[0].game_id, [_leg(m, "yes", m.yes_ask) for m in ms],
                           qty, cost, float(qty), roi, league_of(ms[0]) == "nfl"))
    return out


def kalshi_ladders(markets: list[KalshiMarket]) -> list[Arb]:
    groups: dict[tuple, list[KalshiMarket]] = defaultdict(list)
    for m in markets:
        if m.market_type in ("spread", "total") and m.line is not None and m.status == "active":
            groups[(m.event_ticker, m.market_type, m.team)].append(m)
    out: list[Arb] = []
    for ms in groups.values():
        ms.sort(key=lambda m: m.line)
        for lo, hi in zip(ms, ms[1:]):
            if not hi.yes_bid > lo.yes_ask:
                continue
            qty = int(min(lo.yes_ask_size, hi.no_ask_size))
            if qty < 1:
                continue
            cost = (effective_price(lo.yes_ask, qty, lo.fee_multiplier)
                    + effective_price(hi.no_ask, qty, hi.fee_multiplier)) * qty
            roi = qty / cost - 1
            if roi > 0:
                out.append(Arb("kalshi_ladder", lo.game_id,
                               [_leg(lo, "yes", lo.yes_ask), _leg(hi, "no", hi.no_ask)],
                               qty, cost, float(qty), roi, False))
    return out


def cross_venue(best: dict[str, tuple[float, float]]) -> tuple[float, dict[str, float]] | None:
    """best: venue -> (decimal for side A or 0, decimal for side B or 0)."""
    a = max(((v, d[0]) for v, d in best.items() if d[0] > 1), key=lambda x: x[1], default=None)
    b = max(((v, d[1]) for v, d in best.items() if d[1] > 1), key=lambda x: x[1], default=None)
    if a is None or b is None:
        return None
    s = 1 / a[1] + 1 / b[1]
    if s >= 1:
        return None
    return 1 / s - 1, {a[0]: (1 / a[1]) / s, b[0]: (1 / b[1]) / s}


def filter_arbs(arbs: list[Arb], max_roi: float) -> list[Arb]:
    return [a for a in arbs if a.roi <= max_roi and a.qty >= 1]
