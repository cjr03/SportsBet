"""Odds format conversion."""


def american_to_decimal(a: float) -> float:
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def decimal_to_american(d: float) -> int:
    if d >= 2:
        return round((d - 1) * 100)
    return round(-100 / (d - 1))


def implied(d: float) -> float:
    return 1 / d
