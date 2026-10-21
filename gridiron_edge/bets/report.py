"""Performance report: ROI net of fees, CLV, calibration, drawdown, drift. Plain dicts for the UI.

All money is integer cents. ROI and Brier use graded bets (won/lost); refunds are counted apart.
Drawdown is on settled trading P&L only (payout - stake - fee, in settled_at order): deposits,
withdrawals and open stakes are excluded, and open bets are not marked to market.
"""
import json
import math
from collections import defaultdict
from collections.abc import Iterable

Z = 1.96


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _legs(row) -> list[dict]:
    try:
        return json.loads(_get(row, "legs_json", "[]"))
    except (TypeError, ValueError):
        return []


def _cost(row) -> int:
    return _get(row, "stake_cents", 0) + _get(row, "fee_cents", 0)


def _profit(row) -> int:
    return _get(row, "payout_cents", 0) - _cost(row)


def _graded(rows) -> list:
    return [r for r in rows if _get(r, "result") in ("won", "lost")]


def segment(row) -> str:
    """'<league> <ml|spread|total|prop|prizepicks>' or 'arb'; the drift and gate unit."""
    kind = _get(row, "kind")
    if kind == "arb":
        return "arb"
    legs = _legs(row)
    leg = legs[0] if legs else {}
    league = (leg.get("game_id") or "nfl").split(":")[0]
    if kind == "pp_entry":
        return f"{league} prizepicks"
    if kind == "kalshi_prop":
        return f"{league} prop"
    if leg.get("line") is None:
        return f"{league} ml"
    return f"{league} {'total' if leg.get('team') is None else 'spread'}"


def roi_stats(rows: Iterable) -> dict:
    """Fee-inclusive ROI = sum(profit)/sum(cost), with a delta-method CI for the ratio."""
    g = _graded(rows)
    cost = sum(_cost(r) for r in g)
    profit = sum(_profit(r) for r in g)
    n = len(g)
    out = {"n": n, "cost_cents": cost, "profit_cents": profit, "roi": None, "se": None,
           "ci_lo": None, "ci_hi": None, "conclusive": False}
    if not cost:
        return out
    roi = profit / cost
    out["roi"] = roi
    if n >= 2:
        resid = sum((_profit(r) - roi * _cost(r)) ** 2 for r in g)
        se = math.sqrt(n / (n - 1) * resid) / cost
        out.update(se=se, ci_lo=roi - Z * se, ci_hi=roi + Z * se,
                   conclusive=se > 0 and (roi - Z * se > 0 or roi + Z * se < 0))
    return out


def clv_stats(values: Iterable[float]) -> dict:
    v = [x for x in values if x is not None]
    n = len(v)
    out = {"n": n, "mean": None, "se": None, "t": None, "lo": None, "hi": None, "pos_share": None}
    if not n:
        return out
    mean = sum(v) / n
    out.update(mean=mean, pos_share=sum(x > 0 for x in v) / n)
    if n >= 2:
        se = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) / math.sqrt(n)
        out.update(se=se, t=mean / se if se > 0 else None, lo=mean - 2 * se, hi=mean + 2 * se)
    return out


def _won_event(row) -> int:
    """Outcome of the event p_win forecasts: for PrizePicks that is a profit (mult > 1), not any payout."""
    if _get(row, "kind") == "pp_entry":
        return int(_get(row, "payout_cents", 0) > _get(row, "stake_cents", 0))
    return int(_get(row, "result") == "won")


def brier_by_kind(rows: Iterable) -> dict[str, dict]:
    """Brier score per bet kind (arbs excluded: p_win=1 by construction)."""
    acc: dict[str, list[float]] = defaultdict(list)
    for r in _graded(rows):
        p = _get(r, "p_win")
        if p is None or _get(r, "kind") == "arb":
            continue
        acc[_get(r, "kind")].append((p - _won_event(r)) ** 2)
    return {k: {"n": len(v), "brier": sum(v) / len(v)} for k, v in acc.items()}


def brier(rows: Iterable) -> float | None:
    pts = [(_get(r, "p_win") - _won_event(r)) ** 2 for r in _graded(rows)
           if _get(r, "p_win") is not None and _get(r, "kind") != "arb"]
    return sum(pts) / len(pts) if pts else None


def drawdown(rows: Iterable, starting_cents: int = 0) -> dict:
    """Max and current drawdown of cumulative settled P&L; pct is of peak (starting + P&L) equity."""
    settled = sorted((r for r in rows if _get(r, "result") in ("won", "lost", "refund")),
                     key=lambda r: _get(r, "settled_at", ""))
    pnl = peak = 0
    max_dd = 0
    max_pct = 0.0
    for r in settled:
        pnl += _profit(r)
        peak = max(peak, pnl)
        dd = peak - pnl
        max_dd = max(max_dd, dd)
        eq = starting_cents + peak
        if eq > 0:
            max_pct = max(max_pct, dd / eq)
    eq = starting_cents + peak
    return {"pnl_cents": pnl, "peak_cents": peak, "max_dd_cents": max_dd, "current_dd_cents": peak - pnl,
            "max_dd_pct": max_pct, "current_dd_pct": (peak - pnl) / eq if eq > 0 else 0.0}
