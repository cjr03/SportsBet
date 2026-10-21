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
from gridiron_edge.clients.kalshi import series_type
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
    payout_min: float | None = None  # guaranteed payout over every final score (defaults to payout)
    payout_max: float | None = None  # best case (a middle's "$2 if ...")

    def __post_init__(self) -> None:
        if self.payout_min is None:
            self.payout_min = self.payout
        if self.payout_max is None:
            self.payout_max = self.payout


def team_label(team: str | None, league: str | None = "nfl", names: d.Names = d.DEFAULT_NAMES) -> str:
    """Team code -> display name ("PHI" -> "Eagles", CFB codes via `names`); unknown codes as is."""
    return names.team(league, team) if team else ""


def contract_text(market_type: str, team: str | None, line: float | None, away: str, home: str,
                  league: str | None = "nfl", names: d.Names = d.DEFAULT_NAMES) -> str:
    """Plain-English outcome a Kalshi contract's "Yes" pays on ("Eagles win by 4 or more points").
    A spread with no known team is "" (never a total): callers drop it."""
    t = team_label(team, league, names)
    if market_type == "ml" or line is None:
        return f"{t} win"
    if market_type == "total":
        over = f"over {line:g} points" if line % 1 else f"{math.floor(line) + 1} or more points"
        return f"{team_label(away, league, names)} vs {team_label(home, league, names)} total {over}"
    if not team:
        return ""
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
        if series_type(lo.ticker) == "total":
            return f"the total lands on exactly {points(a)}" if a == b else f"the total lands between {a} and {b} points"
        if a >= 1:
            return f"{name(lo.team)} win by {points(a)}" if a == b else f"{name(lo.team)} win by {a} to {b} points"
        if b < 0:
            return f"{name(lo.team)} lose by {-b} to {points(-a)}" if a != b else f"{name(lo.team)} lose by {points(-a)}"
        top = "a tie" if b == 0 else f"winning by {points(b)}"
        if a < 0:
            return f"{name(lo.team)} finish anywhere from losing by {-a} to {top}"
        return "the game ends in a tie" if b == 0 else f"{name(lo.team)} tie or win by up to {points(b)}"
    x, y = legs  # cross: NO on each team's spread; margin between -floor(y) and floor(x)

    def win(code: str | None, n: int) -> str:
        t = team_label(code, league, names)
        return f"{'an' if t[:1].upper() in 'AEIOU' else 'a'} {t} win by {n}"

    fy, fx = math.floor(y.line or 0), math.floor(x.line or 0)
    return f"the game ends anywhere from {win(y.team, fy)} to {win(x.team, fx)}, ties included"


def has_tie_split(rules: str) -> bool:
    return bool(rules) and _TIE_SPLIT.search(rules) is not None


def ml_tie_risk(m: KalshiMarket, league: str) -> bool:
    """An NFL moneyline can lose on a tie unless its rules split ties 50/50 (the clause is often
    in rules_secondary)."""
    return league == "nfl" and not has_tie_split(_rules(m))


def _rules(m: KalshiMarket) -> str:
    return f"{m.rules_primary} {m.rules_secondary}".strip()


_MAX_MARGIN, _MAX_TOTAL = 80, 150


def _ties_possible(m: KalshiMarket) -> bool:
    """NFL games can end tied; college games (overtime until decided) cannot."""
    return not m.event_ticker.startswith("KXNCAAF")


def payoff_range(legs: list[tuple[KalshiMarket, str]], ties: bool) -> tuple[float, float] | None:
    """Min and max payout per pair (in $) over every final score; None if a leg can't be evaluated
    (a prop, a moneyline or spread with no team, a strike with no line, more than two teams)."""
    teams: list[str] = []
    for m, _ in legs:
        if m.market_type == "prop":
            return None
        if m.market_type in ("ml", "spread"):
            if not m.team:
                return None
            if m.team not in teams:
                teams.append(m.team)
        if m.market_type in ("spread", "total") and m.line is None:
            return None
    if len(teams) > 2:
        return None
    first = teams[0] if teams else None
    # payouts only change at the strikes, so check the margins next to each strike (both parities,
    # closest to zero gives the widest choice of totals) plus around zero and the extremes
    cand = {-_MAX_MARGIN, _MAX_MARGIN, *range(-2, 3)}
    for m, _ in legs:
        if m.market_type == "spread":
            b = math.floor(m.line)
            for k in (b, b + 1):
                cand.update(x for j in range(-2, 3) for x in (k + j, -(k + j)))
    margins = sorted(x for x in cand if -_MAX_MARGIN <= x <= _MAX_MARGIN and (ties or x != 0))
    tcand = {0, 1, _MAX_TOTAL - 1, _MAX_TOTAL}  # likewise for the totals: next to each total strike
    for m, _ in legs:
        if m.market_type == "total":
            b = math.floor(m.line)
            tcand.update(range(b - 2, b + 4))
    splits = [has_tie_split(_rules(m)) if m.market_type == "ml" else False for m, _ in legs]

    def yes(m: KalshiMarket, split: bool, margin: int, total: int) -> float:
        if m.market_type == "total":
            return 1.0 if total > m.line else 0.0
        mine = margin if m.team == first else -margin
        if m.market_type == "spread":
            return 1.0 if mine > m.line else 0.0
        if mine == 0:
            return 0.5 if split else 0.0
        return 1.0 if mine > 0 else 0.0

    lo, hi = math.inf, -math.inf
    for margin in margins:
        low = abs(margin)
        totals = {t for t in tcand if low <= t <= _MAX_TOTAL and (t - low) % 2 == 0} | {low, low + 2}
        for total in totals:
            pay = 0.0
            for (m, side), split in zip(legs, splits):
                y = yes(m, split, margin, total)
                if m.market_type == "ml" and margin == 0 and split:
                    pay += 0.5
                else:
                    pay += y if side == "yes" else 1.0 - y
            lo, hi = min(lo, pay), max(hi, pay)
    return (lo, hi) if lo <= hi else None


def _leg(m: KalshiMarket, side: str, price: float) -> Leg:
    return Leg("kalshi", f"{m.ticker} {side.upper()}", side, price,
               price, m.game_id, ticker=m.ticker, line=m.line, team=m.team, fee_multiplier=m.fee_multiplier)


def _ask(m: KalshiMarket, side: str) -> tuple[float, float]:
    return (m.yes_ask, m.yes_ask_size) if side == "yes" else (m.no_ask, m.no_ask_size)


def _pair(kind: ArbKind, a: KalshiMarket, sa: str, b: KalshiMarket, sb: str, tie_risk: bool = False,
          ties: bool | None = None) -> Arb | None:
    """Two legs that together always pay at least $1 (checked over every final score): cost at the
    shared depth, fees per order. ties: can the game end tied (default: not a college game)."""
    (pa, da), (pb, db) = _ask(a, sa), _ask(b, sb)
    if pa <= 0 or pb <= 0:  # a zero ask is a missing quote, not a free contract
        return None
    qty = int(min(da, db))
    if qty < 1:
        return None
    cost = (effective_price(pa, qty, a.fee_multiplier) + effective_price(pb, qty, b.fee_multiplier)) * qty
    if cost <= 0:
        return None
    rng = payoff_range([(a, sa), (b, sb)], _ties_possible(a) if ties is None else ties)
    if rng is None or rng[0] < 1 - 1e-9:
        return None
    return Arb(kind, a.game_id, [_leg(a, sa, pa), _leg(b, sb, pb)], qty, cost, qty * rng[0], qty / cost - 1,
               tie_risk, min(a.fetched_at, b.fetched_at), qty * rng[0], qty * rng[1])


def _ml_groups(markets: list[KalshiMarket]) -> list[list[KalshiMarket]]:
    groups: dict[str, list[KalshiMarket]] = defaultdict(list)
    for m in markets:
        if m.market_type == "ml" and m.status == "active":
            groups[m.event_ticker].append(m)
    return [ms for ms in groups.values()
            if len(ms) == 2 and not (ms[0].team and ms[1].team and ms[0].team == ms[1].team)]


def kalshi_ml_pairs(markets: list[KalshiMarket], league_of: Callable[[KalshiMarket], str]) -> list[Arb]:
    """YES on both teams: pays $1 unless a tie settles both NO (no 50/50 clause in the rules)."""
    out = []
    for a, b in _ml_groups(markets):
        tie = ml_tie_risk(a, league_of(a)) or ml_tie_risk(b, league_of(b))
        arb = _pair("kalshi_ml_pair", a, "yes", b, "yes", tie, ties=league_of(a) == "nfl")
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
            if m.market_type == "spread" and not m.team:
                continue  # an unknown team's spread can't be laddered (mixing teams can lose it all)
            groups[(m.event_ticker, m.market_type, m.team if m.market_type == "spread" else None)].append(m)
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
