"""PrizePicks math: payout tables, EV, Kelly, entry builder, and the DNP/tie revert ladder."""
import itertools
import math
from dataclasses import dataclass
from typing import Literal

from scipy.optimize import brentq, minimize_scalar

from gridiron_edge.config import Settings, parse_pp_payouts, parse_pp_structure

Structure = tuple[int, Literal["power", "flex"]]

PAYOUTS: dict[Structure, dict[int, float]] = {
    (2, "power"): {2: 3},
    (3, "power"): {3: 6},
    (4, "power"): {4: 10},
    (5, "power"): {5: 20},
    (6, "power"): {6: 37.5},
    (2, "flex"): {2: 2, 1: 0.5},
    (3, "flex"): {3: 3, 2: 1},
    (4, "flex"): {4: 6, 3: 1.5},
    (5, "flex"): {5: 10, 4: 2, 3: 0.4},
    (6, "flex"): {6: 25, 5: 2, 4: 0.4},
}
ALLOWED: list[Structure] = [(3, "power"), (4, "flex"), (5, "flex"), (6, "flex"), (5, "power"), (6, "power")]

_COMBO_LIMIT = 200_000
Payouts = dict[Structure, dict[int, float]]


def _parse_structure(text: str) -> Structure:
    return parse_pp_structure(text)  # type: ignore[return-value]


def payout_table(s: Settings) -> Payouts:
    """Payout multipliers from settings: the fixed tables (or a JSON override), and in arena
    mode only a perfect card pays (the group prize for a non-perfect card is unknowable)."""
    table = {st: dict(tiers) for st, tiers in PAYOUTS.items()}
    if s.pp_payouts_json:
        table.update(parse_pp_payouts(s.pp_payouts_json))  # type: ignore[arg-type]
    if s.pp_mode == "arena":
        table = {(n, kind): {n: tiers[n]} if n in tiers else {} for (n, kind), tiers in table.items()}
    return table


def allowed_structures(s: Settings) -> list[Structure]:
    return [_parse_structure(x) for x in s.pp_structures]
_MAX_FRACTION = 0.99


@dataclass(frozen=True)
class Pick:
    player: str
    team: str
    game_id: str
    stat: str
    line: float
    side: Literal["over", "under"]
    p: float
    anchored: bool


def hit_distribution(ps: list[float]) -> list[float]:
    """Poisson-binomial: probability of exactly k hits, k = 0..len(ps)."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for k, q in enumerate(dist):
            nxt[k] += q * (1.0 - p)
            nxt[k + 1] += q * p
        dist = nxt
    return dist


def entry_ev(ps: list[float], structure: Structure, payouts=PAYOUTS) -> float:
    dist = hit_distribution(ps)
    return sum(dist[k] * m for k, m in payouts[structure].items()) - 1.0


def breakeven(structure: Structure, payouts=PAYOUTS) -> float:
    """Common per-pick probability at which the entry's EV is zero."""
    n = structure[0]
    return brentq(lambda p: entry_ev([p] * n, structure, payouts), 0.3, 0.9)


def kelly_entry(ps: list[float], structure: Structure, payouts=PAYOUTS) -> tuple[float, float]:
    """(f*, G*): bankroll fraction maximizing expected log growth, and that growth."""
    dist = hit_distribution(ps)
    table = payouts[structure]

    def neg_growth(f: float) -> float:
        # Hit counts outside the table pay 0, losing the whole stake (log(1 - f)).
        g = sum(q * math.log(1.0 + f * (table.get(k, 0.0) - 1.0)) for k, q in enumerate(dist) if q > 0)
        return -g

    res = minimize_scalar(neg_growth, bounds=(0.0, _MAX_FRACTION), method="bounded")
    f, g = float(res.x), float(-res.fun)
    if g <= 0:
        return 0.0, 0.0
    return f, g


def _valid(legs) -> bool:
    return len({p.game_id for p in legs}) == len(legs) and len({p.team for p in legs}) >= 2


def _greedy(qual: list[Pick], n: int) -> list[list[Pick]]:
    """Candidate entries that contain the optimum for n legs.

    Payouts never fall with more hits, so growth rises with every pick's p: the top n per-game
    bests dominate any valid entry. If they are one team, the optimum is some pick x plus the
    top n-1 per-game bests from other games; try every x.
    """
    per_game: dict[str, Pick] = {}
    for pk in qual:
        if pk.game_id not in per_game or pk.p > per_game[pk.game_id].p:
            per_game[pk.game_id] = pk
    ranked = sorted(per_game.values(), key=lambda pk: pk.p, reverse=True)
    top = ranked[:n]
    if len(top) == n and _valid(top):
        return [top]
    out = []
    for x in qual:
        rest = [pk for pk in ranked if pk.game_id != x.game_id][:n - 1]
        if len(rest) == n - 1 and _valid([x, *rest]):
            out.append([x, *rest])
    return out


def build_best_entry(picks: list[Pick], s: Settings, allowed=ALLOWED, max_brute: int = 25,
                     payouts: Payouts = PAYOUTS, brute: bool = False
                     ) -> tuple[list[Pick], Structure, float, float] | None:
    """Best entry by full-Kelly growth: (legs, structure, EV, growth). Greedy per structure
    (exact under independent picks); brute=True enumerates combinations instead."""
    best_per_player: dict[str, Pick] = {}
    for pk in picks:
        cur = best_per_player.get(pk.player)
        if cur is None or pk.p > cur.p:
            best_per_player[pk.player] = pk
    pool = sorted(best_per_player.values(), key=lambda pk: pk.p, reverse=True)[:max_brute]

    best: tuple[list[Pick], Structure, float, float] | None = None
    for st in allowed:
        n = st[0]
        if st not in payouts or n not in payouts[st] or payouts[st][n] <= 1:
            continue
        be = breakeven(st, payouts)
        qual = [pk for pk in pool if pk.p >= be + (s.pp_margin_market if pk.anchored else s.pp_margin_model)]
        if len(qual) < n:
            continue
        if not brute:
            candidates = _greedy(qual, n)
        elif math.comb(len(qual), n) > _COMBO_LIMIT:
            chosen: list[Pick] = []
            games: set[str] = set()
            for pk in qual:  # already sorted by p descending
                if pk.game_id not in games:
                    chosen.append(pk)
                    games.add(pk.game_id)
                if len(chosen) == n:
                    break
            candidates = [chosen] if len(chosen) == n and _valid(chosen) else []
        else:
            candidates = (list(c) for c in itertools.combinations(qual, n) if _valid(c))
        for legs in candidates:
            ps = [pk.p for pk in legs]
            ev = entry_ev(ps, st, payouts)
            if ev <= 0:
                continue
            _, g = kelly_entry(ps, st, payouts)
            if best is None or g > best[3]:
                best = (legs, st, ev, g)
    return best


def revert(structure: Structure, dnps: int, ties: int, remaining_teams: int) -> Structure | None:
    """Structure an entry reverts to after DNPs/ties; None means the entry is refunded."""
    n, kind = structure
    if dnps > 0 and (n == 2 or remaining_teams < 2):
        return None
    m = n - dnps - ties
    if m < 2:
        return None
    if m == n:
        return structure
    # A flex that drops to two picks becomes a 2-pick power play.
    return (m, "power" if m == 2 else kind)
