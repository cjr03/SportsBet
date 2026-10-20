"""Kalshi taker fees, fee-adjusted prices, EV and the pre-trade filters."""
import math

from gridiron_edge.config import Settings


def taker_fee(contracts: int, price: float, multiplier: float = 1.0) -> float:
    """Taker fee in dollars: ceil-to-cent of 0.07 * C * P * (1 - P) * multiplier."""
    raw = 0.07 * contracts * price * (1.0 - price) * multiplier
    return math.ceil(round(raw * 100, 9)) / 100


def effective_price(price: float, contracts: int, multiplier: float = 1.0) -> float:
    """Price per contract including the per-contract share of the taker fee."""
    return price + taker_fee(contracts, price, multiplier) / contracts


def kalshi_ev(p_fair: float, p_eff: float) -> float:
    return p_fair / p_eff - 1.0


def passes_filters(p_fair: float, price: float, p_eff: float, size_available: float,
                   qty: int, s: Settings, status: str) -> tuple[bool, str]:
    """Check status, price band, EV, edge, depth in order; reason names the first failure."""
    if status != "active":
        return False, "status"
    if not s.kalshi_price_lo <= price <= s.kalshi_price_hi:
        return False, "price_band"
    if kalshi_ev(p_fair, p_eff) < s.kalshi_min_ev:
        return False, "ev"
    if p_fair - p_eff < s.kalshi_min_edge:
        return False, "edge"
    if size_available < qty:
        return False, "depth"
    return True, "ok"
