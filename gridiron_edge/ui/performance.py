"""Performance tab text: results by bet type, profit today / this week / all time, money in and out,
the last settled bets, and dollar labels for the chart. Pure."""
import math
from datetime import datetime

from gridiron_edge import describe as d

RESULT_WORDS = {"won": "won", "lost": "lost", "refund": "refunded"}


def signed(cents: int) -> str:
    """'+$4.20', '-$3.10', '$0.00'."""
    if cents == 0:
        return d.money(0)
    return ("+" if cents > 0 else "-") + d.money(abs(cents))


def _pct(x: float | None) -> str:
    return "" if x is None else f"{x * 100:+.1f}%"


TYPE_HEAD = ("Bet type", "Bets", "Won-lost-refunded", "Money bet", "Profit", "Return")
_W = (18, 5, 18, 11, 11, 8)


def _row(cells: tuple) -> str:
    first, *rest = cells
    return f"{first:<{_W[0]}}" + "".join(f"{c:>{w}}" for c, w in zip(rest, _W[1:]))


def type_table_lines(table: list[dict]) -> list[str]:
    """Aligned rows (about 71 characters wide) with a total row; one sentence when nothing is settled."""
    if not table:
        return ["No settled bets yet, so there is nothing to break down by bet type."]
    out = [_row(TYPE_HEAD)]
    for t in table:
        out.append(_row((t["label"], str(t["bets"]), f"{t['won']}-{t['lost']}-{t['refunded']}",
                         d.money(t["cost_cents"]), signed(t["profit_cents"]), _pct(t["roi"]))))
    if len(table) > 1:
        cost, profit = sum(t["cost_cents"] for t in table), sum(t["profit_cents"] for t in table)
        rec = "-".join(str(sum(t[k] for t in table)) for k in ("won", "lost", "refunded"))
        out.append(_row(("All bets", str(sum(t["bets"] for t in table)), rec, d.money(cost), signed(profit),
                         _pct(profit / cost if cost else None))))
    return out


def money_lines(windows: dict[str, int], deposited: int, withdrawn: int) -> list[str]:
    """Profit today / this week / all time and money put in and taken out, one per line."""
    return [f"Profit today: {signed(windows.get('today', 0))}",
            f"Profit this week: {signed(windows.get('week', 0))}",
            f"Profit all time: {signed(windows.get('all', 0))}",
            f"Money deposited: {d.money(deposited)}",
            f"Money withdrawn: {d.money(withdrawn)}"]


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(1, width - 3)].rstrip() + "..."


def recent_lines(rows: list, now: datetime, width: int = 100, names: d.Names = d.DEFAULT_NAMES) -> list[str]:
    """'Sun 4:05 PM PT  won       +$4.20  #12 Eagles to beat the Cowboys' (newest first), cut to width."""
    if not rows:
        return ["No settled bets yet."]
    out = []
    for r in rows:
        try:
            when = f"{d.when(datetime.fromisoformat(r['settled_at']), now)} PT"
        except (TypeError, ValueError):
            when = ""
        profit = (r["payout_cents"] or 0) - r["stake_cents"] - (r["fee_cents"] or 0)
        try:
            what = d.bet_row_text(r, names)
        except (KeyError, TypeError, ValueError):
            what = "bet"
        head = f"{when:<18}{RESULT_WORDS.get(r['result'], r['result']):<10}{signed(profit):>10}  #{r['id']} "
        out.append(_fit(head + what, width))
    return out


def dollar_ticks(lo_cents: float, hi_cents: float, n: int = 5) -> tuple[list[float], list[str]]:
    """About n evenly spaced y-axis ticks in dollars on round steps, labeled like '$1,571'."""
    lo, hi = lo_cents / 100, hi_cents / 100
    if hi - lo < 1e-9:
        return [lo], [f"${lo:,.0f}"]
    raw = (hi - lo) / max(1, n - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step) * step
    vals = []
    v = start
    while v <= hi + 1e-9:
        vals.append(round(v, 2))
        v += step
    if not vals:
        vals = [lo, hi]
    cents = step < 1
    return vals, [f"${x:,.2f}" if cents else f"${x:,.0f}" for x in vals]
