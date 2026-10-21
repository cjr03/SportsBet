"""Plain-English, plain-ASCII text messages for recommendations (one action per text, <= 480 chars)."""
import unicodedata
from dataclasses import replace
from zoneinfo import ZoneInfo

from gridiron_edge import describe as d
from gridiron_edge.strategy.arb import middle_window
from gridiron_edge.strategy.prizepicks import PAYOUTS, Payouts
from gridiron_edge.types import Recommendation

PT = ZoneInfo("America/Los_Angeles")
MAX_LEN = 480
SHORT_STATS = {"passing": "pass", "rushing": "rush", "receiving": "rec", "touchdowns": "TDs"}
TEST_TEXT = ("Gridiron Edge test text. If your phone did not buzz, set up the backup phone alerts "
             "(NTFY_TOPIC in .env).")


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _tail(rec: Recommendation) -> str:
    return f"When placed, type: confirm {rec.id}"


def _kalshi(rec: Recommendation, tz: ZoneInfo, names: d.Names) -> str:
    leg = rec.legs[0]
    contract = d.leg_text(replace(leg, side="yes"), names)
    side = "Yes" if leg.side == "yes" else "No"
    happens = "" if leg.side == "yes" else " (you win if it does NOT happen)"
    ours = "We put this at" if leg.side == "yes" else "We put your chance of winning at"
    return (f'Bet idea #{rec.id} (Kalshi): Buy {rec.qty} "{side}" contracts on "{contract}"{happens} '
            f"at {round(leg.price * 100)} cents. Cost {d.money(d.cost_cents(rec))} + "
            f"{d.money(d.fee_cents(rec))} fee. {ours} {d.pct(leg.p_fair)}; "
            f"the price implies {d.pct(leg.price)}. Expected profit about "
            f"{d.money(d.expected_profit_cents(rec))}. Place before {d.clock(rec.expires_at, tz)} PT. "
            + _tail(rec))


def payouts_text(table: dict[int, float]) -> str:
    """'pays 10 times your entry for 5 right, 2 times for 4, 0.4 times for 3' (paying tiers only)."""
    tiers = [(hits, m) for hits, m in sorted(table.items(), reverse=True) if m > 0]
    if not tiers:
        return ""
    def pays(m: float, first: bool) -> str:
        return "your entry back" if m == 1 else f"{m:g} times" + (" your entry" if first else "")

    (h0, m0), rest = tiers[0], tiers[1:]
    return f"pays {pays(m0, True)} for {h0} right" + "".join(f", {pays(m, False)} for {h}" for h, m in rest)


def _pp(rec: Recommendation, tz: ZoneInfo, names: d.Names, short: int = 0,
        payouts: Payouts | None = None) -> str:
    def pick(i, leg):
        text = d.leg_text(leg, names)
        if short:
            for long, abbr in SHORT_STATS.items():
                text = text.replace(long, abbr)
        return f"{i}) {text}" + ("" if short > 1 else f" ({d.pct(leg.p_fair)})")

    n, _, kind = (rec.structure or "").partition("-")
    table = (payouts or PAYOUTS).get((int(n), kind), {}) if n.isdigit() else {}
    pays = payouts_text(table)
    check = f"Check the app says it {pays}. " if pays and short < 3 else "Check the payouts in the app. "
    picks = " ".join(pick(i, leg) for i, leg in enumerate(rec.legs, 1))
    return (f"Bet idea #{rec.id} (PrizePicks {d.structure_text(rec.structure)}, "
            f"{d.money(rec.stake_cents)}): {picks}. Expected profit about "
            f"{d.money(d.expected_profit_cents(rec))}. {check}"
            f"Lines as of {d.clock(rec.snapshot_ts, tz)} PT. " + _tail(rec))


def _cents(price: float) -> str:
    return f"{round(price * 100)} cents"


def _pair(rec: Recommendation, tz: ZoneInfo, names: d.Names, legging: bool = True) -> str:
    """Arbs ("Locked profit") and middles ("Not risk-free"): each leg as the outcome its side pays on,
    the cost and payback, and which leg to buy first with its price limit."""
    buys = " AND ".join(f'{rec.qty} "{"Yes" if g.side == "yes" else "No"}" on '
                        f'"{d.leg_text(replace(g, side="yes"), names)}"' for g in rec.legs)
    q = rec.qty or 0
    cost = f"Total {d.money(d.cost_cents(rec))} + {d.money(d.fee_cents(rec))} fees"
    a, b = (rec.legs + [rec.legs[-1]])[:2]
    first = d.leg_text(replace(a, side="yes"), names)
    if rec.kind == "arb":
        who = "whichever team wins" if rec.structure == "kalshi_ml_pair" else "whatever happens"
        tie = " Risk: a tie pays nothing." if rec.tie_risk else ""
        text = (f"Locked profit #{rec.id} (Kalshi): Buy {buys}. {cost}; you get {d.money(q * 100)} back {who} "
                f"(profit about {d.money(d.expected_profit_cents(rec))}).{tie} ")
        order = (f'Buy the "{first}" leg first at {_cents(a.worst_price)} or less, then the other at '
                 f"{_cents(b.worst_price)} or less. " if a.worst_price and b.worst_price
                 else "Place both right away; prices move fast. ")
    else:
        text = (f"Not risk-free #{rec.id} (Kalshi): Buy {buys}. {cost}; you get {d.money(q * 100)} back, "
                f"or {d.money(2 * q * 100)} if {middle_window(rec.legs, names)} (about {d.pct(rec.p_win)} chance). ")
        order = (f'Buy the "{first}" leg first, then the other at {_cents(b.worst_price)} or less. '
                 if b.worst_price else "Place both right away; prices move fast. ")
    return text + (order if legging else "") + _tail(rec)


def format_message(rec: Recommendation, tz: ZoneInfo = PT, names: d.Names = d.DEFAULT_NAMES,
                   payouts: Payouts | None = None) -> str:
    """payouts: the PrizePicks payout table in use (strategy.prizepicks.payout_table(settings))."""
    if rec.kind.startswith("kalshi_"):
        text = _kalshi(rec, tz, names)
    elif rec.kind == "pp_entry":
        text = _pp(rec, tz, names, payouts=payouts)
        for short in (1, 2, 3):  # long entries: abbreviate stats, drop the per-pick chances, then the payouts
            if len(_ascii(text)) <= MAX_LEN:
                break
            text = _pp(rec, tz, names, short, payouts)
    elif rec.kind in ("arb", "middle"):
        text = _pair(rec, tz, names)
        if len(_ascii(text)) > MAX_LEN:  # drop the legging sentence before cutting anything
            text = _pair(rec, tz, names, legging=False)
    else:
        raise ValueError(f"cannot format kind {rec.kind!r}")
    text = _ascii(text)
    if len(text) > MAX_LEN:  # keep the action line intact
        tail = " " + _tail(rec)
        text = text[:MAX_LEN - len(tail) - 3].rstrip() + "..." + tail
    return text


def notice(text: str) -> str:
    """A text that is not a bet idea (test, warnings): plain ASCII, cut to MAX_LEN."""
    text = _ascii(text)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 3].rstrip() + "..."


def drift_message(label: str, n: int, mean: float, seg: str) -> str:
    """The text sent when a kind of bet is paused because its prices fell behind the closing price."""
    return notice(f"Gridiron Edge paused {label}: the last {n} got prices {abs(mean) * 100:.1f}% worse than "
                  f"the closing price on average, so new ones are shown but not texted. "
                  f"To turn them back on, type: drift reset {seg}")
