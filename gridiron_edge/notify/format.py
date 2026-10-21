"""Plain-ASCII text messages for recommendations."""
import unicodedata
from zoneinfo import ZoneInfo

from gridiron_edge.strategy.arb import middle_window
from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.types import Recommendation

PT = ZoneInfo("America/Los_Angeles")


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _stat(stat: str | None) -> str:
    return (stat or "").replace("_", " ").title()


_MAX_TEXT = 480


def _cents(price: float) -> str:
    return f"{round(price * 100)} cents"


def _pair_text(rec: Recommendation) -> str:
    """Arbs ("Locked profit") and middles ("Not risk-free") in the design's plain-English shape."""
    q = rec.qty or 0
    a, b = (rec.legs + [rec.legs[-1]])[:2]
    price = q * sum(leg.price for leg in rec.legs)
    fees = sum(taker_fee(q, leg.price, leg.fee_multiplier) for leg in rec.legs)
    buy = " AND ".join(f'{q} "{leg.side.title()}" on "{leg.description}"' for leg in rec.legs)
    cost = f"Total ${price:.2f} + ${fees:.2f} fees"
    end = f"When placed, type: confirm {rec.id}"
    if rec.kind == "arb":
        head = f"Locked profit #{rec.id} (Kalshi): Buy {buy}. "
        body = f"{cost}; you get ${q:.2f} back whichever way it ends (profit about ${q - price - fees:.2f}). "
        if rec.tie_risk:
            body += "Tie risk: if the game ends in a tie, both legs can lose. "
        order = (f'Buy the "{a.description}" leg first at {_cents(a.worst_price)} or less, then the other at '
                 f"{_cents(b.worst_price)} or less. " if a.worst_price and b.worst_price
                 else "Place both right away; prices move fast. ")
    else:
        head = f"Not risk-free #{rec.id} (Kalshi): Buy {buy}. "
        body = (f"{cost}; you get ${q:.2f} back, or ${2 * q:.2f} if {middle_window(rec.legs)} "
                f"(about {rec.p_win:.0%} chance). ")
        order = (f'Buy the "{a.description}" leg first, then the other at {_cents(b.worst_price)} or less. '
                 if b.worst_price else "Place both right away; prices move fast. ")
    text = head + body + order + end
    return text if len(text) <= _MAX_TEXT else head + body + end


def format_message(rec: Recommendation, tz: ZoneInfo = PT) -> str:
    hhmm = f"{rec.snapshot_ts.astimezone(tz):%H:%M}"
    if rec.kind.startswith("kalshi_"):
        leg = rec.legs[0]
        # the same fee confirm debits (the market's multiplier), shown apart from the stake
        fee = taker_fee(rec.qty, leg.price, leg.fee_multiplier) if rec.qty else 0.0
        text = (f"GE KALSHI BUY {leg.side.upper()} {leg.description} ({leg.ticker}) "
                f"@ ${leg.price:.2f} x{rec.qty} = ${rec.stake_cents / 100:.2f} + ${fee:.2f} fee "
                f"| fair {leg.p_fair:.1%} EV +{rec.ev:.1%} | verify availability before placing | {hhmm} PT | id {rec.id}")
    elif rec.kind == "pp_entry":
        n, _, kind = (rec.structure or "").partition("-")
        picks = " ".join(
            f"{i}) {leg.player} {_stat(leg.stat)} {'MORE' if leg.side == 'over' else 'LESS'} "
            f"{leg.line} ({leg.p_fair:.0%})" for i, leg in enumerate(rec.legs, 1))
        text = (f"GE PP {n}-{kind.upper()} ${rec.stake_cents / 100:.0f} | EV +{rec.ev:.1%} | {picks} "
                f"| lines {hhmm} PT | confirm payout in app | id {rec.id}")
    elif rec.kind in ("arb", "middle"):
        return _ascii(_pair_text(rec))
    else:
        raise ValueError(f"cannot format kind {rec.kind!r}")
    return _ascii(text)
