"""Arbitrage detection: Kalshi ML pairs, spread/total ladders, cross-team spreads, cross-venue;
plus ladder middles (pairs costing a little over $1 that pay $2 when the score lands between)."""

import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from gridiron_edge import describe as d
from gridiron_edge.strategy.kalshi_pricing import effective_price
from gridiron_edge.types import KalshiMarket, Leg

ArbKind = Literal["kalshi_ml_pair", "kalshi_ml_no_pair", "kalshi_ladder", "kalshi_cross_spread",
                  "cross_venue", "ladder_middle", "cross_middle"]
# Kalshi NFL rules settle a tie at $0.50 per side; then YES+YES and NO+NO always pay $1
_TIE_SPLIT = re.compile(r"\btie[ds]?\b.{0,120}?(50\s*/\s*50|\$?0?\.50\b|50\s*(?:cents|%|percent)|half)",
                        re.IGNORECASE | re.DOTALL)


@dataclass
class Arb:
    kind: ArbKind
    game_id: str | None
    legs: list[Leg]
    qty: int
    cost: float
    payout: float
    roi: float
    tie_risk: bool
    fetched_at: datetime | None = None  # oldest quote among the legs


def team_label(team: str | None, league: str | None = "nfl", names: d.Names = d.DEFAULT_NAMES) -> str:
    """Team code -> display name ("PHI" -> "Eagles", CFB codes via `names`); unknown codes as is."""
    return names.team(league, team) if team else ""


def contract_text(market_type: str, team: str | None, line: float | None, away: str, home: str,
                  league: str | None = "nfl", names: d.Names = d.DEFAULT_NAMES) -> str:
    """Plain-English outcome a Kalshi contract's "Yes" pays on ("Eagles win by 4 or more points")."""
    t = team_label(team, league, names)
    if market_type == "ml" or line is None:
        return f"{t} win"
    if market_type == "total" or team is None:
        over = f"over {line:g} points" if line % 1 else f"{math.floor(line) + 1} or more points"
        return f"{team_label(away, league, names)} vs {team_label(home, league, names)} total {over}"
    return d.margin_text(t, line)


def middle_window(legs: list[Leg], names: d.Names = d.DEFAULT_NAMES) -> str:
    """Where both legs of a middle pay, in plain English ("the Eagles win by 3 to 6 points")."""
    league = d.parse_game_id(legs[0].game_id)[0] or "nfl"

    def name(code: str | None) -> str:  # "the Eagles", but plain "Alabama" for a school
        return ("the " if league == "nfl" else "") + team_label(code, league, names)

    def points(n: int) -> str:
        return f"{n} point{'s' * (abs(n) != 1)}"

    yes = [leg for leg in legs if leg.side == "yes"]
    if yes and legs[0].line is not None and legs[1].line is not None:  # ladder: YES low + NO high
        lo, hi = yes[0], next(leg for leg in legs if leg is not yes[0])
        a, b = math.floor(lo.line) + 1, math.floor(hi.line)
        if lo.team is None:
            return f"the total lands on exactly {points(a)}" if a == b else f"the total lands between {a} and {b} points"
        if a >= 1:
            return f"{name(lo.team)} win by {points(a)}" if a == b else f"{name(lo.team)} win by {a} to {b} points"
        return f"{name(lo.team)} margin ends between {a} and {b} points (minus means they lose)"
    x, y = legs  # cross: NO on each team's spread; margin between -floor(y) and floor(x)
    return (f"the final margin is between {name(y.team)} by {points(math.floor(y.line or 0))} "
            f"and {name(x.team)} by {points(math.floor(x.line or 0))} (a tie counts)")


def has_tie_split(rules: str) -> bool:
    return bool(rules) and _TIE_SPLIT.search(rules) is not None


def ml_tie_risk(m: KalshiMarket, league: str) -> bool:
    """An NFL moneyline can lose on a tie unless its rules split ties 50/50."""
    return league == "nfl" and not has_tie_split(m.rules_primary)


def _leg(m: KalshiMarket, side: str, price: float) -> Leg:
    return Leg("kalshi", f"{m.ticker} {side.upper()}", side, price,
               price, m.game_id, ticker=m.ticker, line=m.line, team=m.team, fee_multiplier=m.fee_multiplier)


def _ask(m: KalshiMarket, side: str) -> tuple[float, float]:
    return (m.yes_ask, m.yes_ask_size) if side == "yes" else (m.no_ask, m.no_ask_size)


def _pair(kind: ArbKind, a: KalshiMarket, sa: str, b: KalshiMarket, sb: str, tie_risk: bool = False
          ) -> Arb | None:
    """Two legs that together always pay at least $1: cost at the shared depth, fees per order."""
    (pa, da), (pb, db) = _ask(a, sa), _ask(b, sb)
    if pa <= 0 or pb <= 0:  # a zero ask is a missing quote, not a free contract
        return None
    qty = int(min(da, db))
    if qty < 1:
        return None
    cost = (effective_price(pa, qty, a.fee_multiplier) + effective_price(pb, qty, b.fee_multiplier)) * qty
    if cost <= 0:
        return None
    return Arb(kind, a.game_id, [_leg(a, sa, pa), _leg(b, sb, pb)], qty, cost, float(qty), qty / cost - 1,
               tie_risk, min(a.fetched_at, b.fetched_at))


def _ml_groups(markets: list[KalshiMarket]) -> list[list[KalshiMarket]]:
    groups: dict[str, list[KalshiMarket]] = defaultdict(list)
    for m in markets:
        if m.market_type == "ml" and m.status == "active":
            groups[m.event_ticker].append(m)
    return [ms for ms in groups.values() if len(ms) == 2]


def kalshi_ml_pairs(markets: list[KalshiMarket], league_of: Callable[[KalshiMarket], str]) -> list[Arb]:
    """YES on both teams: pays $1 unless a tie settles both NO (no 50/50 clause in the rules)."""
    out = []
    for a, b in _ml_groups(markets):
        tie = ml_tie_risk(a, league_of(a)) or ml_tie_risk(b, league_of(b))
        arb = _pair("kalshi_ml_pair", a, "yes", b, "yes", tie)
        if arb is not None and arb.roi > 0:
            out.append(arb)
    return out


def kalshi_ml_no_pairs(markets: list[KalshiMarket]) -> list[Arb]:
    """NO on both teams: one team wins (one NO pays), a split tie pays 50c each, a NO-NO tie pays $2."""
    out = []
    for a, b in _ml_groups(markets):
        arb = _pair("kalshi_ml_no_pair", a, "no", b, "no")
        if arb is not None and arb.roi > 0:
            out.append(arb)
    return out


def _ladders(markets: list[KalshiMarket]) -> list[list[KalshiMarket]]:
    groups: dict[tuple, list[KalshiMarket]] = defaultdict(list)
    for m in markets:
        if m.market_type in ("spread", "total") and m.line is not None and m.status == "active":
            groups[(m.event_ticker, m.market_type, m.team)].append(m)
    return [sorted(ms, key=lambda m: m.line) for ms in groups.values()]


def kalshi_ladders(markets: list[KalshiMarket]) -> list[Arb]:
    """YES at a lower strike + NO at a higher strike always pays >= $1 (both pay in between).
    Every pair i < j is checked: for each higher strike, the cheapest YES ask below it."""
    out: list[Arb] = []
    for ms in _ladders(markets):
        cheapest: KalshiMarket | None = None
        for hi in ms:
            if cheapest is not None and cheapest.line < hi.line:
                arb = _pair("kalshi_ladder", cheapest, "yes", hi, "no")
                if arb is not None and arb.roi > 0:
                    out.append(arb)
            if hi.yes_ask > 0 and (cheapest is None or hi.yes_ask < cheapest.yes_ask):
                cheapest = hi
    return out


def _cross_groups(markets: list[KalshiMarket]) -> list[tuple[list[KalshiMarket], list[KalshiMarket]]]:
    """Per game, the spread markets of each of the two teams."""
    games: dict[str, dict[str, list[KalshiMarket]]] = defaultdict(lambda: defaultdict(list))
    for m in markets:
        if m.market_type == "spread" and m.line is not None and m.team and m.game_id and m.status == "active":
            games[m.game_id][m.team].append(m)
    return [tuple(by_team.values()) for by_team in games.values() if len(by_team) == 2]


def kalshi_cross_spreads(markets: list[KalshiMarket]) -> list[Arb]:
    """NO "A wins by > x" + NO "B wins by > y" always pays >= $1 when x >= -y; the cheapest pair per game."""
    out = []
    for team_a, team_b in _cross_groups(markets):
        best: Arb | None = None
        for a in team_a:
            for b in team_b:
                if a.line < -b.line:
                    continue
                arb = _pair("kalshi_cross_spread", a, "no", b, "no")
                if arb is not None and arb.roi > 0 and (best is None or arb.roi > best.roi):
                    best = arb
        if best is not None:
            out.append(best)
    return out


def kalshi_middles(markets: list[KalshiMarket], max_over: float) -> list[Arb]:
    """Pairs that pay $1 always and $2 when the margin lands between the strikes, costing between
    $1 and $1 + max_over per pair (not risk-free: the edge rests on the margin model)."""
    out = []
    pairs = [("ladder_middle", lo, "yes", hi, "no") for ms in _ladders(markets)
             for i, lo in enumerate(ms) for hi in ms[i + 1:] if lo.line < hi.line]
    pairs += [("cross_middle", a, "no", b, "no") for ta, tb in _cross_groups(markets)
              for a in ta for b in tb if a.line >= -b.line]
    for kind, a, sa, b, sb in pairs:
        arb = _pair(kind, a, sa, b, sb)
        if arb is not None and arb.qty <= arb.cost <= arb.qty * (1 + max_over):
            out.append(replace(arb, roi=arb.qty / arb.cost - 1))
    return out


def cross_venue(best: dict[str, tuple[float, float]]) -> tuple[float, dict[str, float]] | None:
    """best: venue -> (decimal for side A or 0, decimal for side B or 0)."""
    a = max(((v, d[0]) for v, d in best.items() if d[0] > 1), key=lambda x: x[1], default=None)
    b = max(((v, d[1]) for v, d in best.items() if d[1] > 1), key=lambda x: x[1], default=None)
    if a is None or b is None:
        return None
    s = 1 / a[1] + 1 / b[1]
    if s >= 1:
        return None
    return 1 / s - 1, {a[0]: (1 / a[1]) / s, b[0]: (1 / b[1]) / s}


def filter_arbs(arbs: list[Arb], max_roi: float, min_roi: float = 0.0) -> list[Arb]:
    return [a for a in arbs if min_roi <= a.roi <= max_roi and a.qty >= 1]
