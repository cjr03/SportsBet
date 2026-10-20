"""Closing-line value (price_eff and p_close_fair are probabilities)."""


def clv(price_eff: float | None, p_close_fair: float | None) -> float | None:
    """Relative gain of the closing fair probability over the price paid."""
    if price_eff is None or p_close_fair is None or price_eff <= 0:
        return None
    return p_close_fair / price_eff - 1.0
