"""Home-tab logic: which recs can be placed now, open-bet rows, and why PLACE NOW is empty. Pure."""
import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from gridiron_edge import describe as d
from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev, taker_fee
from gridiron_edge.types import Game, KalshiMarket, Leg, Recommendation

OPEN_STATUSES = ("active", "open", "")
StatLookup = Callable[[str, str, str], object]  # (game_id, player, stat) -> value | None | "pending"


def _ask(m: KalshiMarket, side: str) -> float:
    return m.yes_ask if side == "yes" else m.no_ask


def _bid(m: KalshiMarket, side: str) -> float:
    return m.yes_bid if side == "yes" else m.no_bid


def _still_good(rec: Recommendation, markets: dict[str, KalshiMarket], min_ev: float) -> bool:
    """False when a Kalshi leg's market has closed, lost its quote, or moved past the edge."""
    if rec.kind == "pp_entry":
        return True
    cur = []
    for leg in rec.legs:
        m = markets.get(leg.ticker or "")
        if m is None:
            cur.append((leg, leg.price, 1.0))  # not in the latest pull: trust the rec
            continue
        if m.status not in OPEN_STATUSES or _ask(m, leg.side) <= 0:
            return False
        cur.append((leg, _ask(m, leg.side), m.fee_multiplier))
    if all(price <= leg.price for leg, price, _ in cur):
        return True
    qty = rec.qty or 1
    if rec.kind == "arb":
        return sum(effective_price(p, qty, f) for _, p, f in cur) < 1.0
    leg, price, fee_mult = cur[0]
    return kalshi_ev(leg.p_fair, effective_price(price, qty, fee_mult)) >= min_ev


def actionable(recs: Iterable[Recommendation], now: datetime, games: dict[str, Game],
               markets: dict[str, KalshiMarket], min_ev: float) -> list[Recommendation]:
    """Recs the user can place right now, best expected profit first."""
    out = []
    for r in recs:
        if r.observe_only or r.state not in ("RECOMMENDED", "PENDING") or now >= r.expires_at:
            continue
        gs = [games[leg.game_id] for leg in r.legs if leg.game_id in games]
        if any(g.kickoff <= now or g.status in ("live", "final") for g in gs):
            continue
        if _still_good(r, markets, min_ev):
            out.append(r)
    return sorted(out, key=lambda r: (-d.expected_profit_cents(r), r.id or 0))


_HEDGED = ("arb", "middle")


def _gate_keys(rec: Recommendation, games: dict[str, Game]) -> set[tuple[str, str]]:
    """Gate keys a rec answers to: (league, ml|spread|total) for game bets, prop/pp for player bets."""
    out = set()
    for leg in rec.legs:
        g = games.get(leg.game_id or "")
        if g is None:
            continue
        if rec.kind == "kalshi_game":
            out.add((g.league, d.market_type(leg)))
        elif rec.kind == "kalshi_prop":
            out.add((g.league, "prop"))
        elif rec.kind == "pp_entry":
            out |= {(g.league, "prop"), (g.league, "pp")}
    return out


def within_limits(recs: Iterable[Recommendation], games: dict[str, Game], budget,
                  paused: dict[tuple[str, str], bool] | None = None) -> list[Recommendation]:
    """Keep recs (in order) whose stake still fits the cumulative caps after the bets already placed
    and the recs kept before them, and whose market has not been paused since the rec was made.

    budget: a pipeline.Budget (per-game, per-slate, per-day and same-direction headroom in cents).
    Locked-profit pairs and middles are hedged, so the directional caps do not apply to them.
    """
    from gridiron_edge.pipeline import direction_key, slate_window

    paused = paused or {}
    out = []
    for r in recs:
        if any(paused.get(k) for k in _gate_keys(r, games)):
            continue
        if r.kind in _HEDGED or budget is None:
            out.append(r)
            continue
        gids = {leg.game_id for leg in r.legs if leg.game_id}
        known = [games[g] for g in gids if g in games]
        dirs = ([direction_key(leg, games.get(leg.game_id or "")) for leg in r.legs]
                if r.kind in ("kalshi_game", "kalshi_prop") else [])
        room = [budget.day_left(), *(budget.game_left(g) for g in gids),
                *(budget.window_left(slate_window(g)) for g in known),
                *(budget.dir_left(k) for k in dirs if k is not None)]
        if r.stake_cents > min(room):
            continue
        budget.charge(r.stake_cents, gids, dirs)
        out.append(r)
    return out


def _kickoff(rec: Recommendation, games: dict[str, Game]) -> datetime | None:
    ks = [games[leg.game_id].kickoff for leg in rec.legs if leg.game_id in games]
    return min(ks) if ks else None


def place_now_cells(rec: Recommendation, now: datetime, games: dict[str, Game],
                    names: d.Names = d.DEFAULT_NAMES) -> list[str]:
    """id, where, bet, price, stake, expected profit, time left, kickoff."""
    kick = _kickoff(rec, games) or rec.expires_at
    where = ("Kalshi - Locked profit" if rec.kind == "arb" else "Kalshi - Not risk-free" if rec.kind == "middle"
             else d.venue(rec.kind))
    return [str(rec.id), where, d.bet_text(rec, names), d.price_text(rec), d.money(rec.stake_cents),
            d.money(d.expected_profit_cents(rec)), d.countdown(rec.expires_at, now), d.when(kick, now)]


PLACE_NOW_COLUMNS = ("#", "Where", "Bet", "Price", "Stake", "Expected profit", "Time left", "Kickoff")
OPEN_BET_COLUMNS = ("#", "Bet", "Stake", "Game", "Worth now", "Price vs closing")


def game_text(g: Game | None, now: datetime, names: d.Names = d.DEFAULT_NAMES) -> str:
    if g is None:
        return "-"
    away, home = names.team(g.league, g.away), names.team(g.league, g.home)
    score = f"{away} {g.away_pts}, {home} {g.home_pts}"
    if g.status == "live" and g.home_pts is not None and g.away_pts is not None:
        when = f" - {d.period_text(g.period, g.clock)}" if g.period else " - live"
        return score + when
    if g.status == "final" and g.home_pts is not None and g.away_pts is not None:
        return f"Final: {score}"
    return f"kickoff {d.when(g.kickoff, now)} PT"


@dataclass
class OpenBetView:
    bet_id: int
    text: str
    stake: str
    game: str
    worth: str
    close: str
    pnl_cents: int | None = None
    pays: str = ""
    chances: str = ""
    picks: str = ""
    paid_cents: int = 0
    max_payout_cents: int = 0
    expected_cents: int = 0
    paid_text: str = ""  # 'paid $10.00' when the sell line does not already say it
    stake_cents: int = 0  # before fees, as the header counts money in open bets

    def cells(self) -> list[str]:
        return [str(self.bet_id), self.text, self.stake, self.game, self.worth, self.close, self.pays,
                self.chances, self.picks, self.paid_text]


def _pick_result(v, leg: Leg) -> str | None:
    """'hit' / 'missed' / 'pushed' / None (no stats yet) for one PrizePicks pick."""
    if v == "pending" or leg.line is None:
        return None
    if v is None:
        return "missed"
    if abs(float(v) - leg.line) < 1e-9:
        return "pushed"
    return "hit" if (float(v) > leg.line) == (leg.side == "over") else "missed"


def _pp_progress(legs: list[Leg], stat_lookup: StatLookup | None) -> str:
    counts = {"hit": 0, "missed": 0, "pushed": 0, "pending": 0}
    for leg in legs:
        v = stat_lookup(leg.game_id or "", leg.player or "", leg.stat or "") if stat_lookup else "pending"
        counts[_pick_result(v, leg) or "pending"] += 1  # did not play counts as missed: the pick is dead
    words = {"hit": "hit", "missed": "missed", "pushed": "pushed (exactly on the line)", "pending": "pending"}
    return ", ".join(f"{n} {words[k]}" for k, n in counts.items() if n) or "-"


def pick_lines(legs: list[Leg], stat_lookup: StatLookup | None) -> str:
    """Each pick's progress once stats exist: 'Hurts 1 of 1.5 passing touchdowns (missed)'; '' when none do."""
    out, any_stats = [], False
    for leg in legs:
        v = stat_lookup(leg.game_id or "", leg.player or "", leg.stat or "") if stat_lookup else "pending"
        name, line = short_player(leg.player), "?" if leg.line is None else f"{leg.line:g}"
        res = _pick_result(v, leg)
        if v == "pending":
            out.append(f"{name}: no stats yet")
        elif v is None:
            any_stats = True
            out.append(f"{name} did not play (missed)")
        else:
            any_stats = True
            more = "more" if leg.side == "over" else "less"
            out.append(f"{name} {float(v):g} of {line} {d.stat_words(leg.stat)} (needed {more}; {res})")
    return " \u00b7 ".join(out) if any_stats else ""


def _side_mid(m: KalshiMarket, side: str) -> float | None:
    bid, ask = _bid(m, side), _ask(m, side)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return ask or bid or None


def open_bet_view(bet, games: dict[str, Game], markets: dict[str, KalshiMarket], now: datetime,
                  names: d.Names = d.DEFAULT_NAMES, stat_lookup: StatLookup | None = None,
                  payouts=None) -> OpenBetView:
    """One open bet (dict or sqlite Row) for the OPEN BETS list."""
    legs = [Leg(**x) for x in json.loads(bet["legs_json"])]
    paid = bet["stake_cents"] + (bet["fee_cents"] or 0)
    gs = [games.get(leg.game_id or "") for leg in legs]
    game = next((g for g in gs if g is not None and g.status == "live"), None) or \
        next((g for g in gs if g is not None), None)
    worth, pnl, chances, picks = "-", None, "", ""
    qty = bet["qty"] or 0
    if bet["kind"] == "pp_entry":
        from gridiron_edge.strategy.prizepicks import entry_ev
        worth = _pp_progress(legs, stat_lookup)
        picks = pick_lines(legs, stat_lookup)
        top = round(bet["stake_cents"] * pp_top_multiplier(bet["structure"], payouts))
        st, table = _structure(bet["structure"]), _pp_table(payouts)
        ev = entry_ev([leg.p_fair for leg in legs], st, table) if st in table and len(legs) == st[0] else 0.0
        lo, hi, expected = 0, top, round(bet["stake_cents"] * (1 + ev))
        pays = f"pays up to {d.money(top)} if all {len(legs)} hit" if top else ""
    else:
        lo, hi = payout_range_cents(legs, qty, markets, games, bet["kind"] == "arb" and len(legs) == 2)
        if bet["kind"] == "arb":
            expected = lo
        else:
            expected = round(sum(qty * leg.p_fair * 100 for leg in legs))
        pays = payout_text(legs, qty, markets, games, bet["kind"] == "arb" and len(legs) == 2)
        total, no_buyer = 0, False
        for leg in legs:
            m = markets.get(leg.ticker or "")
            if m is None:
                total = None
                break
            bid = _bid(m, leg.side)
            if bid <= 0:
                no_buyer = True
            fee = round(taker_fee(qty, bid, leg.fee_multiplier) * 100) if 0 < bid < 1 else 0
            total += round(qty * bid * 100) - fee
        if not qty:
            worth = "no contracts to sell"
        elif total is None:
            worth = "no current Kalshi price"
        elif no_buyer:
            worth = "no buyer right now"
        else:
            pnl = total - paid
            sign = "+" if pnl >= 0 else "-"
            worth = f"sell now for {d.money(total)} \u00b7 paid {d.money(paid)} \u00b7 {sign}{d.money(abs(pnl))}"
        if len(legs) == 1:
            leg = legs[0]
            m = markets.get(leg.ticker or "")
            mid = _side_mid(m, leg.side) if m is not None else None
            chances = f"our chance {d.pct(leg.p_fair)} when placed"
            if mid is not None:
                chances += f" \u00b7 market says {d.pct(mid)} now"
    worth_paid = "" if pnl is not None else f"paid {d.money(paid)}"
    clv = bet["clv"]
    view = OpenBetView(bet["id"], d.bet_row_text(bet, names), d.money(paid), game_text(game, now, names),
                       worth, "not yet" if clv is None else close_text(clv), pnl, pays, chances, picks,
                       paid, hi, expected, worth_paid, bet["stake_cents"])
    return view


def open_summary(views: list[OpenBetView]) -> list[str] | None:
    """['$29.60 in 2 open bets ($31.00 with fees)', 'up to $100.00 back if all win',
    'about $33.70 expected back (+$2.70) at our chances when placed']."""
    if not views:
        return None
    paid = sum(v.paid_cents for v in views)
    stake = sum(v.stake_cents for v in views)
    top = sum(v.max_payout_cents for v in views)
    exp = sum(v.expected_cents for v in views)
    gain = exp - paid
    sign = "+" if gain >= 0 else "-"
    staked = f"{d.money(stake)} in {_plural(len(views), 'open bet')}"
    if paid != stake:
        staked += f" ({d.money(paid)} with fees)"
    return [staked, f"up to {d.money(top)} back if {'it wins' if len(views) == 1 else 'all win'}",
            f"about {d.money(exp)} expected back ({sign}{d.money(abs(gain))}) at our chances when placed"]


def close_text(clv: float) -> str:
    """'price 0.8% better than closing' / '... worse than closing' (never a minus sign after 'beat')."""
    return f"price {abs(clv):.1%} {'better' if clv >= 0 else 'worse'} than closing"


def empty_reason(now: datetime, *, odds_key: bool, games: list[Game], markets: list[KalshiMarket],
                 gates: dict, next_check: datetime | None, names: d.Names = d.DEFAULT_NAMES) -> str:
    """Why PLACE NOW is empty, in one or two plain sentences."""
    tail = ""
    if next_check is not None:
        mins = math.ceil((next_check - now).total_seconds() / 60)
        tail = " Next check in 1 min." if mins <= 1 else f" Next check in {mins} min."
        if next_check <= now:
            tail = " Checking now."
    upcoming = sorted((g for g in games if g.kickoff > now and g.status == "scheduled"),
                      key=lambda g: g.kickoff)
    if not odds_key:
        return ("The Odds API key isn't set (ODDS_API_KEY in .env), so there are no sportsbook lines "
                "to compare prices against." + tail)
    if not upcoming:
        return "No games on the schedule yet." + tail
    priced = {m.game_id for m in markets if m.game_id}
    if not any(g.game_id in priced for g in upcoming):
        return f"Kalshi lines for the {d.when(upcoming[0].kickoff, now)} games aren't open yet." + tail
    leagues = {g.league for g in upcoming}
    if all(gates.get((lg, m), False) for lg in leagues for m in ("ml", "spread", "total")):
        return ("Every market is watch-only (shown, not texted) until our numbers prove themselves, "
                "so nothing is suggested as a bet." + tail)
    return "No good bets right now." + tail


# -- the info line under the header ------------------------------------------------------------------
def age_text(t: datetime | None, now: datetime, what: str) -> str:
    """'Kalshi prices 4 min old' / 'no Kalshi prices yet'."""
    if t is None:
        return f"no {what} yet"
    mins = max(0, int((now - t).total_seconds() // 60))
    if mins < 1:
        return f"{what} under 1 min old"
    if mins < 60:
        return f"{what} {mins} min old"
    hrs = mins // 60
    if hrs < 24:
        return f"{what} {hrs} hr old"
    days = hrs // 24
    return f"{what} {days} day{'s' * (days != 1)} old"


def record_text(won: int, lost: int, refunded: int = 0) -> str:
    """'Today: 2 won, 1 lost' / 'Today: no bets settled yet'."""
    parts = [f"{n} {w}" for n, w in ((won, "won"), (lost, "lost"), (refunded, "refunded")) if n]
    return "Today: " + (", ".join(parts) if parts else "no bets settled yet")


def info_segments(now: datetime, *, record: tuple[int, int, int], day_left: int | None, day_cap: int | None,
                  next_check: datetime | None, kalshi_at: datetime | None, books_at: datetime | None,
                  props_at: datetime | None) -> list[str]:
    """Today's record, room left under the daily limit, the next price check and how old each price feed is."""
    out = [record_text(*record)]
    if day_left is not None and day_cap is not None:
        out.append(f"{d.money(day_left)} of today's {d.money(day_cap)} limit left")
    if next_check is not None:
        out.append("checking prices now" if next_check <= now else f"next check {d.clock(next_check)} PT")
    out += [age_text(kalshi_at, now, "Kalshi prices"), age_text(books_at, now, "sportsbook lines"),
            age_text(props_at, now, "player lines")]
    return out


def pack(segments: list[str], width: int, sep: str = " · ") -> list[str]:
    """Segments joined with sep, wrapped only between segments so each fits in `width` where it can."""
    lines: list[str] = []
    for seg in segments:
        if lines and len(lines[-1]) + len(sep) + len(seg) <= width:
            lines[-1] += sep + seg
        else:
            lines.append(seg)
    return lines


# -- PLACE NOW detail line ----------------------------------------------------------------------------
def max_price(leg: Leg, qty: int, min_ev: float) -> float | None:
    """Highest whole-cent price at which this leg still clears the minimum edge after the fee."""
    best = None
    for c in range(1, 100):
        p = c / 100
        if kalshi_ev(leg.p_fair, effective_price(p, max(qty, 1), leg.fee_multiplier)) >= min_ev:
            best = p
    return best


def _cents_text(p: float) -> str:
    c = round(p * 100)
    return f"{c} cent{'s' * (c != 1)}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def short_player(name: str | None) -> str:
    """'Amon-Ra St. Brown' -> 'St. Brown', 'Kenneth Walker III' -> 'Walker', 'Jalen Hurts' -> 'Hurts'."""
    words = (name or "?").split()
    while len(words) > 1 and words[-1].rstrip(".").upper() in ("JR", "SR", "II", "III", "IV", "V"):
        words.pop()
    if len(words) > 2 and words[-2].rstrip(".").lower() in ("st", "san", "van", "de", "la", "le", "du", "von"):
        return " ".join(words[-2:])
    return words[-1]


def source_short(rec: Recommendation) -> str:
    """Where our number came from, in a few words."""
    why = rec.why or {}
    if rec.kind == "arb":
        return "from the prices alone (no prediction needed)"
    if rec.kind == "middle":
        return "from sportsbook lines and past final scores"
    src = (why.get("fair_source") or "").removeprefix("anchored-")
    if src:
        books = list(dict.fromkeys(why.get("books") or []))
        others = len([b for b in books if b != "pinnacle"])
        text = {"pinnacle": "Pinnacle" + (f" and {_plural(others, 'other sportsbook')}" if others else ""),
                "exchanges": "betting exchanges",
                "mid": f"{_plural(len(books), 'low-margin sportsbook')}" if books else "low-margin sportsbooks",
                "median": f"the middle of {_plural(len(books), 'sportsbook')}" if books else "the sportsbooks",
                }.get(src, "sportsbooks")
        if (why.get("model_weight") or 0) > 0 and why.get("p_model") is not None:
            text += " plus our team ratings"
        return "from " + text
    if why.get("kalshi_mid") is not None:
        mine = "sportsbook player lines" if why.get("book_line") is not None else "our player projection"
        return f"from {mine} and Kalshi traders"
    if rec.kind == "pp_entry":
        anchored = [leg.p_mkt is not None for leg in rec.legs]
        if all(anchored):
            return "from sportsbook player lines"
        return "from our player projection" if not any(anchored) else \
            "from sportsbook player lines and our player projection"
    if rec.kind == "kalshi_prop":
        text = str(why.get("source") or "")
        return "from our player projection" if "projection" in text and "sportsbook" not in text else \
            "from sportsbook player lines"
    return "from sportsbook lines"


def payout_range_cents(legs: list[Leg], qty: int, markets: dict[str, KalshiMarket], games: dict[str, Game],
                       tie_risk: bool = False, ties: bool | None = None) -> tuple[int, int]:
    """Worst and best payout (cents) of a Kalshi position over every final score (ties: None = whether
    this league's games can end tied; False = leave ties out)."""
    ms = [markets.get(leg.ticker or "") for leg in legs]
    if len(legs) > 1 and all(m is not None for m in ms):
        from gridiron_edge.strategy.arb import payoff_range
        g = games.get(legs[0].game_id or "")
        if ties is None:
            ties = g.league == "nfl" if g is not None else not ms[0].event_ticker.startswith("KXNCAAF")
        rng = payoff_range([(m, leg.side) for m, leg in zip(ms, legs)], ties)
        if rng is not None:
            return round(rng[0] * qty * 100), round(rng[1] * qty * 100)
    if len(legs) == 1:
        return 0, qty * 100
    return (0 if tie_risk and ties is not False else qty * 100), qty * 100


def payout_text(legs: list[Leg], qty: int, markets: dict[str, KalshiMarket], games: dict[str, Game],
                tie_risk: bool = False) -> str:
    """'pays $20.00 whichever way it goes' / 'pays $20.00 at worst, $40.00 at best', calling out a tie."""
    lo, hi = payout_range_cents(legs, qty, markets, games, tie_risk)
    if len(legs) == 1:
        return f"pays {d.money(hi)} if it wins"
    if lo == hi:
        return f"pays {d.money(lo)} whichever way it goes"
    lo2, hi2 = payout_range_cents(legs, qty, markets, games, tie_risk, ties=False)
    if (lo2, hi2) != (lo, hi):  # only a tie drags the worst case down
        main = f"pays {d.money(lo2)} whichever team wins" if lo2 == hi2 else \
            f"pays {d.money(lo2)} at worst, {d.money(hi2)} at best"
        return f"{main} ({d.money(lo)} on a tie)"
    return f"pays {d.money(lo)} at worst, {d.money(hi)} at best"


def _pp_table(payouts):
    if payouts is None:
        from gridiron_edge.strategy.prizepicks import PAYOUTS
        return PAYOUTS
    return payouts


def _structure(text: str | None):
    n, _, kind = (text or "").partition("-")
    try:
        return int(n), kind
    except ValueError:
        return None


def pp_needed(structure: str | None, payouts=None) -> float | None:
    """Per-pick chance this PrizePicks entry type needs to break even, or None if it never does."""
    from gridiron_edge.strategy.prizepicks import breakeven
    table, st = _pp_table(payouts), _structure(structure)
    if st is None or st not in table:
        return None
    try:
        return breakeven(st, table)
    except ValueError:
        return None


def pp_top_multiplier(structure: str | None, payouts=None) -> float:
    st = _structure(structure)
    tiers = _pp_table(payouts).get(st, {}) if st else {}
    return max(tiers.values(), default=0.0)


def place_now_detail(rec: Recommendation, markets: dict[str, KalshiMarket], games: dict[str, Game],
                     min_ev: float, payouts=None) -> str:
    """The second detail line of a PLACE NOW row: chances, how many and up to what price, payout, source."""
    qty = rec.qty or 0
    if rec.kind == "pp_entry":
        picks = ", ".join(f"{short_player(leg.player)} {d.pct(leg.p_fair)}" for leg in rec.legs)
        need = pp_needed(rec.structure, payouts)
        top = round(rec.stake_cents * pp_top_multiplier(rec.structure, payouts))
        parts = [f"our chance per pick: {picks}"]
        if need is not None:
            parts.append(f"breaks even at about {d.pct(need)} per pick")
        if top:
            parts.append(f"pays up to {d.money(top)} if all {len(rec.legs)} hit")
        return " · ".join(parts + [source_short(rec)])
    if rec.kind in ("arb", "middle"):
        pays = payout_text(rec.legs, qty, markets, games, rec.tie_risk)
        capped = all(leg.worst_price for leg in rec.legs)
        limits = " and ".join(_cents_text((leg.worst_price if capped else None) or leg.price) for leg in rec.legs)
        parts = [f"{_plural(qty, 'contract')} of each side" + (f", pay up to {limits}" if capped else f" at {limits}"),
                 pays]
        if rec.kind == "middle":
            parts.insert(0, f"both sides pay about {d.pct(rec.p_win)} of the time")
        return " · ".join(parts + [source_short(rec)])
    leg = rec.legs[0]
    parts = [f"our chance {d.pct(leg.p_fair)}", f"price says {d.pct(leg.price)}"]
    if leg.p_mkt is not None and rec.kind == "kalshi_game":
        parts.append(f"sportsbooks say {d.pct(leg.p_mkt)}")
    top = max_price(leg, qty, min_ev)
    parts.append(f"{_plural(qty, 'contract')}" + (f", pay up to {_cents_text(top)}" if top else ""))
    parts.append(f"pays {d.money(qty * 100)} if it wins")
    return " · ".join(parts + [source_short(rec)])


# -- WATCHING ---------------------------------------------------------------------------------------
WATCH_LIMIT = 8


def watch_reason(rec: Recommendation, games: dict[str, Game], gates: dict, paused: dict,
                 middles_live: bool = True) -> str:
    """Why a watch-only idea is shown but not texted, in plain words."""
    why = rec.why or {}
    if rec.kind == "middle" and not middles_live:
        return "pairs that are not risk-free are watch-only for now"
    keys = _gate_keys(rec, games)
    if any(paused.get(k) for k in keys):
        return "paused after poor recent results"
    if rec.kind == "kalshi_prop" and why.get("kalshi_mid") is not None and why.get("book_line") is None:
        return "our player projection is still being tested"
    if rec.kind == "pp_entry" and any(leg.p_mkt is None for leg in rec.legs):
        return "our player projection is still being tested"
    gated = [k for k in keys if gates.get(k)]
    if gated:
        if any(lg == "cfb" for lg, _ in gated):
            return "our college model is still being tested"
        return "our model is still being tested for this kind of bet"
    return "watch-only until our numbers prove themselves"


def watching(recs: Iterable[Recommendation], now: datetime, games: dict[str, Game]) -> list[Recommendation]:
    """Watch-only ideas still worth a look (latest per idea, game not started), best expected profit first."""
    latest: dict[str, Recommendation] = {}
    for r in recs:
        if not r.observe_only or r.state != "LOGGED" or now >= r.expires_at:
            continue
        gs = [games[leg.game_id] for leg in r.legs if leg.game_id in games]
        if any(g.kickoff <= now or g.status in ("live", "final") for g in gs):
            continue
        cur = latest.get(r.idea_key)
        if cur is None or (r.id or 0) > (cur.id or 0):
            latest[r.idea_key] = r
    return sorted(latest.values(), key=lambda r: (-r.ev, r.id or 0))


def watch_cells(rec: Recommendation, now: datetime, games: dict[str, Game], reason: str,
                names: d.Names = d.DEFAULT_NAMES) -> tuple[str, str]:
    """(headline, detail) for one WATCHING row."""
    where = "PrizePicks" if rec.kind == "pp_entry" else "Kalshi"
    head = f"#{rec.id} {where}  {d.bet_text(rec, names)}"
    c = round(rec.ev * 100)
    parts = [] if rec.kind == "pp_entry" else [d.price_text(rec)]
    if rec.kind in ("kalshi_game", "kalshi_prop"):
        parts.append(f"our chance {d.pct(rec.legs[0].p_fair)}")
    parts.append(f"about {c} cent{'s' * (c != 1)} profit per $1 bet")
    kick = _kickoff(rec, games)
    if kick is not None:
        parts.append(f"kickoff {d.when(kick, now)} PT")
    parts.append(f"not texted: {reason}")
    return head, "    " + " · ".join(parts)


def watch_summary(shown: int, total: int) -> str | None:
    if total == 0:
        return "Nothing on the watch list."
    if total > shown:
        more = total - shown
        return f"{_plural(more, 'more idea')} on the watch list not shown."
    return None
