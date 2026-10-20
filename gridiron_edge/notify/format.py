"""Plain-ASCII text messages for recommendations."""
import unicodedata
from zoneinfo import ZoneInfo

from gridiron_edge.types import Recommendation

PT = ZoneInfo("America/Los_Angeles")


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _stat(stat: str | None) -> str:
    return (stat or "").replace("_", " ").title()


def format_message(rec: Recommendation, tz: ZoneInfo = PT) -> str:
    hhmm = f"{rec.snapshot_ts.astimezone(tz):%H:%M}"
    if rec.kind.startswith("kalshi_"):
        leg = rec.legs[0]
        text = (f"GE KALSHI BUY {leg.side.upper()} {leg.description} ({leg.ticker}) "
                f"@ ${leg.price:.2f} x{rec.qty} = ${rec.stake_cents / 100:.2f} w/fee "
                f"| fair {leg.p_fair:.1%} EV +{rec.ev:.1%} | {hhmm} PT | id {rec.id}")
    elif rec.kind == "pp_entry":
        n, _, kind = (rec.structure or "").partition("-")
        picks = " ".join(
            f"{i}) {leg.player} {_stat(leg.stat)} {'MORE' if leg.side == 'over' else 'LESS'} "
            f"{leg.line} ({leg.p_fair:.0%})" for i, leg in enumerate(rec.legs, 1))
        text = (f"GE PP {n}-{kind.upper()} ${rec.stake_cents / 100:.0f} | EV +{rec.ev:.1%} | {picks} "
                f"| lines {hhmm} PT | confirm payout in app | id {rec.id}")
    elif rec.kind == "arb":
        legs = " + ".join(leg.description for leg in rec.legs)
        tie = " tie risk |" if rec.tie_risk else ""
        text = f"GE ARB KALSHI +{rec.ev:.1%} | {legs} x{rec.qty} |{tie} verify now | {hhmm} PT | id {rec.id}"
    else:
        raise ValueError(f"cannot format kind {rec.kind!r}")
    return _ascii(text)
