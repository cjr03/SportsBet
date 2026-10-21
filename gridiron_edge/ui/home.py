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
    return f"Kickoff {d.when(g.kickoff, now)} PT"


@dataclass
class OpenBetView:
    bet_id: int
    text: str
    stake: str
    game: str
    worth: str
    close: str
    pnl_cents: int | None = None

    def cells(self) -> list[str]:
        return [str(self.bet_id), self.text, self.stake, self.game, self.worth, self.close]


def _pp_progress(legs: list[Leg], stat_lookup: StatLookup | None) -> str:
    hit = missed = pushed = pending = 0
    for leg in legs:
        v = stat_lookup(leg.game_id or "", leg.player or "", leg.stat or "") if stat_lookup else "pending"
        if v == "pending" or leg.line is None:
            pending += 1
        elif v is None:
            missed += 1  # did not play: PrizePicks reverts the entry, but the pick is dead
        elif abs(float(v) - leg.line) < 1e-9:
            pushed += 1  # landed exactly on the line: PrizePicks drops the pick (a push), neither side wins
        elif (float(v) > leg.line) == (leg.side == "over"):
            hit += 1
        else:
            missed += 1
    parts = [f"{n} {w}" for n, w in ((hit, "hit"), (missed, "missed"), (pushed, "pushed (exactly on the line)"),
                                     (pending, "pending")) if n]
    return ", ".join(parts) or "-"


def open_bet_view(bet, games: dict[str, Game], markets: dict[str, KalshiMarket], now: datetime,
                  names: d.Names = d.DEFAULT_NAMES, stat_lookup: StatLookup | None = None) -> OpenBetView:
    """One open bet (dict or sqlite Row) for the OPEN BETS list."""
    legs = [Leg(**x) for x in json.loads(bet["legs_json"])]
    paid = bet["stake_cents"] + (bet["fee_cents"] or 0)
    gs = [games.get(leg.game_id or "") for leg in legs]
    game = next((g for g in gs if g is not None and g.status == "live"), None) or \
        next((g for g in gs if g is not None), None)
    worth, pnl = "-", None
    if bet["kind"] == "pp_entry":
        worth = _pp_progress(legs, stat_lookup)
    elif bet["qty"]:
        qty, total = bet["qty"], 0
        for leg in legs:
            m = markets.get(leg.ticker or "")
            if m is None:
                total = None
                break
            bid = _bid(m, leg.side)
            fee = round(taker_fee(qty, bid, leg.fee_multiplier) * 100) if 0 < bid < 1 else 0
            total += round(qty * bid * 100) - fee
        if total is not None:
            pnl = total - paid
            sign = "+" if pnl >= 0 else "-"
            worth = f"{d.money(total)} ({sign}{d.money(abs(pnl))})"
    clv = bet["clv"]
    return OpenBetView(bet["id"], d.bet_row_text(bet, names), d.money(paid), game_text(game, now, names),
                       worth, "not yet" if clv is None else close_text(clv), pnl)


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
