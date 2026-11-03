"""PrizePicks math: payout tables, EV, Kelly, entry builder, and the DNP/tie revert ladder."""
import heapq
import itertools
import math
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.optimize import brentq, minimize_scalar
from scipy.stats import norm, qmc

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
ALLOWED: list[Structure] = [(3, "power"), (4, "flex"), (5, "flex"), (6, "flex"), (5, "power"), (6, "power"),
                            (2, "power"), (4, "power"), (3, "flex")]

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
    return _kelly([hit_distribution(ps)], payouts[structure])


def _kelly(dists: list[list[float]], table: dict[int, float]) -> tuple[float, float]:
    """Fraction maximizing the worst expected log growth over several hit distributions."""

    def growth(f: float, dist: list[float]) -> float:
        # Hit counts outside the table pay 0, losing the whole stake (log(1 - f)).
        return sum(q * math.log(1.0 + f * (table.get(k, 0.0) - 1.0)) for k, q in enumerate(dist) if q > 0)

    res = minimize_scalar(lambda f: -min(growth(f, d) for d in dists), bounds=(0.0, _MAX_FRACTION),
                          method="bounded")
    f, g = float(res.x), float(-res.fun)
    if g <= 0:
        return 0.0, 0.0
    return f, g


# -- same-game correlation: Gaussian copula, scored at the worst of rho in {-|r|, 0, +|r|} --------
_GL_X, _GL_W = np.polynomial.legendre.leggauss(24)  # fixed nodes: exact and deterministic
_RHO_CLIP = 0.95


def corr_sign(a: Pick, b: Pick) -> int:
    """Sign of the correlation between two picks' hits: 0 across games; in one game +1 when both lean
    the same way (a QB and his own receiver, or both QBs in a shootout), -1 opposite ways.

    Every same-game pair gets a sign so the robust set {-|r|, 0, +|r|} prices all of them; signs of
    the form side_i * side_j also keep 3-pick blocks a valid correlation matrix."""
    if a.game_id != b.game_id:
        return 0
    return 1 if a.side == b.side else -1


def _phi2(x: float, y: float, r: float) -> float:
    q = 1.0 - r * r
    return math.exp(-(x * x - 2 * r * x * y + y * y) / (2 * q)) / (2 * math.pi * math.sqrt(q))


def mvn_cdf(a: list[float], r: list[list[float]]) -> float:
    """P(Z_i < a_i for all i), Z standard normal with correlation r, for 1-3 dimensions.

    Plackett's identity integrated along r(t) = t*r with Gauss-Legendre nodes: dP/dr_ij is the
    pair's density times the third variable's conditional CDF."""
    n = len(a)
    base = math.prod(norm.cdf(x) for x in a)
    if n == 1:
        return base
    if n > 3:
        raise ValueError("at most 3 correlated picks per game")
    total = 0.0
    for x, w in zip(_GL_X, _GL_W):
        t = 0.5 * (x + 1.0)
        d = 0.0
        for i, j in itertools.combinations(range(n), 2):
            rij = r[i][j]
            if rij == 0.0:
                continue
            term = rij * _phi2(a[i], a[j], t * rij)
            if n == 3:
                k = 3 - i - j
                ri, rj, r_ij = t * r[k][i], t * r[k][j], t * rij
                det = 1.0 - r_ij * r_ij
                bi, bj = (ri - r_ij * rj) / det, (rj - r_ij * ri) / det  # s @ inverse(pair block)
                var = max(1.0 - (bi * ri + bj * rj), 1e-12)
                term *= norm.cdf((a[k] - (bi * a[i] + bj * a[j])) / math.sqrt(var))
            d += term
        total += 0.5 * w * d
    return base + total


def _corr_matrix(block: list[Pick], rho: float) -> list[list[float]]:
    r = [[1.0 if i == j else rho * corr_sign(a, b) for j, b in enumerate(block)] for i, a in enumerate(block)]
    while np.linalg.eigvalsh(np.array(r)).min() < 1e-6:  # mixed signs at large rho: shrink to valid
        r = [[v if i == j else 0.9 * v for j, v in enumerate(row)] for i, row in enumerate(r)]
    return r


_QMC_POINTS = 2 ** 16  # fixed-seed Sobol points for blocks of more than 3 picks: deterministic, within ~1e-3
_QMC_SEED = 7


def _qmc_block_distribution(a: list[float], r: list[list[float]]) -> list[float]:
    """Hit-count distribution of one block from a fixed quasi-random normal sample with correlation r:
    the exact 3-pick integral does not extend to 4-6 picks from one game, so those are sampled."""
    m = len(a)
    z = qmc.MultivariateNormalQMC(mean=np.zeros(m), cov=np.array(r), seed=_QMC_SEED).random(_QMC_POINTS)
    hits = (z < np.array(a)).sum(axis=1)
    counts = np.bincount(hits, minlength=m + 1)[: m + 1]
    return [float(c) / _QMC_POINTS for c in counts]


def _block_distribution(block: list[Pick], rho: float) -> list[float]:
    """Hit-count distribution of one game's picks, by inclusion-exclusion over joint hit CDFs (up to
    3 picks; a fixed quasi-random sample beyond that)."""
    m = len(block)
    a = [norm.ppf(min(max(pk.p, 1e-9), 1 - 1e-9)) for pk in block]
    r = _corr_matrix(block, rho)
    if m > 3:
        return _qmc_block_distribution(a, r)
    all_hit = {(): 1.0}
    for size in range(1, m + 1):
        for sub in itertools.combinations(range(m), size):
            all_hit[sub] = mvn_cdf([a[i] for i in sub], [[r[i][j] for j in sub] for i in sub])
    dist = [0.0] * (m + 1)
    for size in range(m + 1):
        for hit in itertools.combinations(range(m), size):
            rest = [i for i in range(m) if i not in hit]
            q = sum((-1) ** extra * all_hit[tuple(sorted(hit + more))]
                    for extra in range(len(rest) + 1) for more in itertools.combinations(rest, extra))
            dist[size] += max(q, 0.0)
    tot = sum(dist)
    return [q / tot for q in dist]


def correlated_hit_distribution(legs: list[Pick], rho: float) -> list[float]:
    """Hit-count distribution with same-game pairs correlated at rho * corr_sign; games independent."""
    rho = max(-_RHO_CLIP, min(_RHO_CLIP, rho))
    by_game: dict[str, list[Pick]] = {}
    for pk in legs:
        by_game.setdefault(pk.game_id, []).append(pk)
    dist = [1.0]
    for block in by_game.values():
        if len(block) == 1 or rho == 0.0 or not any(
                corr_sign(x, y) for x, y in itertools.combinations(block, 2)):
            part = hit_distribution([pk.p for pk in block])
        else:
            part = _block_distribution(block, rho)
        nxt = [0.0] * (len(dist) + len(part) - 1)
        for i, q in enumerate(dist):
            for j, w in enumerate(part):
                nxt[i + j] += q * w
        dist = nxt
    return dist


def robust_distributions(legs: list[Pick], rho: float) -> list[list[float]]:
    """The hit distributions at rho in {-|r|, 0, +|r|}: we don't know the sign or size well."""
    r = abs(rho)
    if r == 0.0 or not any(corr_sign(x, y) for x, y in itertools.combinations(legs, 2)):
        return [hit_distribution([pk.p for pk in legs])]
    return [correlated_hit_distribution(legs, x) for x in (-r, 0.0, r)]


def _ev(dist: list[float], table: dict[int, float]) -> float:
    return sum(dist[k] * m for k, m in table.items() if k < len(dist)) - 1.0


def worst_distribution(legs: list[Pick], structure: Structure, payouts=PAYOUTS, rho: float = 0.0) -> list[float]:
    """The hit distribution (over the robust rho set) with the lowest EV."""
    return min(robust_distributions(legs, rho), key=lambda d: _ev(d, payouts[structure]))


def robust_ev(legs: list[Pick], structure: Structure, payouts=PAYOUTS, rho: float = 0.0) -> float:
    return _ev(worst_distribution(legs, structure, payouts, rho), payouts[structure])


def robust_kelly(legs: list[Pick], structure: Structure, payouts=PAYOUTS, rho: float = 0.0
                 ) -> tuple[float, float]:
    """(f, G): the fraction maximizing the worst-case growth over the robust rho set, and that growth."""
    return _kelly(robust_distributions(legs, rho), payouts[structure])


def _valid(legs, max_per_game: int = 1) -> bool:
    """One pick per player, at most max_per_game from one game, and at least two teams."""
    games = Counter(p.game_id for p in legs)
    return (len({p.player for p in legs}) == len(legs) and max(games.values()) <= max_per_game
            and len({p.team for p in legs}) >= 2)


def _top(qual: list[Pick], n: int, cap: int, taken: tuple[Pick, ...] = ()) -> list[Pick] | None:
    """Best n picks (qual is sorted by p, descending) with at most cap per game, after `taken`."""
    counts = Counter(pk.game_id for pk in taken)
    out: list[Pick] = []
    for pk in qual:
        if pk in taken or counts[pk.game_id] >= cap:
            continue
        out.append(pk)
        counts[pk.game_id] += 1
        if len(out) == n:
            return out
    return None


_SHORTLIST_COMBOS = 3_000  # pick sets looked at per number of picks, strongest picks first
_SHORTLIST = 30  # of those, how many get the full same-game scoring


def _shortlist(qual: list[Pick], n: int, cap: int) -> list[list[Pick]]:
    """Valid n-pick sets of the strongest picks, highest sum of log chances first (the order of
    power-play growth for independent picks; for flex plays only roughly). The pool keeps the
    best cap + 1 picks of each game before the strongest picks are taken, so lines bunched in a
    few games can't crowd out every valid set; it keeps the count of sets under _SHORTLIST_COMBOS."""
    per_game: Counter = Counter()
    spread = []
    for pk in qual:
        if per_game[pk.game_id] <= cap:
            spread.append(pk)
            per_game[pk.game_id] += 1
    if len(spread) < n:
        return []
    m = n
    while m < len(spread) and math.comb(m + 1, n) <= _SHORTLIST_COMBOS:
        m += 1
    top = spread[:m]
    games, teams = [pk.game_id for pk in top], [pk.team for pk in top]
    logs = [math.log(pk.p) for pk in top]
    heap: list[tuple[float, tuple[int, ...]]] = []  # the _SHORTLIST best (sum of logs, indices) so far
    for idx in itertools.combinations(range(m), n):  # indices keep the order (top is sorted by chance)
        if len({teams[i] for i in idx}) < 2 or max(Counter(games[i] for i in idx).values()) > cap:
            continue
        item = (sum(logs[i] for i in idx), tuple(-i for i in idx))
        if len(heap) < _SHORTLIST:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    return [[top[-i] for i in neg] for _, neg in sorted(heap, reverse=True)]


def _greedy(qual: list[Pick], n: int, max_per_game: int = 1) -> list[list[Pick]]:
    """Candidate entries that contain the optimum for n legs under independent picks.

    At most cap picks per game is a partition matroid, so greedy by p gives the set whose sorted
    p's dominate every valid set; growth rises with every pick's p, so it is optimal. If it is one
    team, the optimum is some other-team pick x plus the greedy n-1 after x; try every x. Smaller
    caps are tried too: their spread-out entries may win once same-game correlation is priced in.
    """
    out: list[list[Pick]] = []
    for cap in range(1, max_per_game + 1):
        top = _top(qual, n, cap)
        if top is None:
            continue
        if _valid(top, cap):
            out.append(top)
            continue
        team = top[0].team
        for x in qual:
            if x.team == team:
                continue
            rest = _top(qual, n - 1, cap, (x,))
            if rest is not None and _valid([x, *rest], cap):
                out.append([x, *rest])
    return out


_KEEP_PER_STRUCTURE = 60  # scored entries kept per structure for top_entries to choose among


def _scored_per_structure(picks: list[Pick], s: Settings, allowed, max_brute: int, payouts: Payouts, brute: bool
                          ) -> list[list[tuple[list[Pick], Structure, float, float]]]:
    """For each allowed structure, its profitable candidate entries, best growth first (at most
    _KEEP_PER_STRUCTURE): (legs, structure, EV, growth), EV and growth at the worst case over the
    same-game correlation. Candidates are the greedy sets plus the strongest-picks shortlist (the
    greedy sets are only exact for independent picks), or every combination with brute=True."""
    cap = s.pp_max_per_game
    best_per_player: dict[str, Pick] = {}
    for pk in picks:
        cur = best_per_player.get(pk.player)
        if cur is None or pk.p > cur.p:
            best_per_player[pk.player] = pk
    pool = sorted(best_per_player.values(), key=lambda pk: pk.p, reverse=True)[:max_brute]

    out: list[list[tuple[list[Pick], Structure, float, float]]] = []
    dists_of: dict[frozenset, list[list[float]]] = {}  # 3-power and 3-flex share their pick sets
    shortlists: dict[tuple, list[list[Pick]]] = {}
    for st in allowed:
        n = st[0]
        if st not in payouts or n not in payouts[st] or payouts[st][n] <= 1:
            continue
        try:
            be = breakeven(st, payouts)
        except ValueError:  # an override that never breaks even between 30% and 90% per pick
            continue
        qual = [pk for pk in pool if pk.p >= be + (s.pp_margin_market if pk.anchored else s.pp_margin_model)]
        if len(qual) < n:
            continue
        if not brute:
            key = (n, tuple(qual))  # 4-flex and 4-power often qualify the same picks
            if key not in shortlists:
                shortlists[key] = _shortlist(qual, n, cap)
            candidates = [*_greedy(qual, n, cap), *shortlists[key]]
        elif math.comb(len(qual), n) > _COMBO_LIMIT:
            chosen = _top(qual, n, cap)
            candidates = [chosen] if chosen is not None and _valid(chosen, cap) else []
        else:
            candidates = (list(c) for c in itertools.combinations(qual, n) if _valid(c, cap))
        seen: set[frozenset] = set()
        scored: list[tuple[list[Pick], Structure, float, float]] = []
        for legs in candidates:
            key = frozenset(legs)
            if not brute:  # greedy and shortlist sets overlap; combinations never repeat
                if key in seen:
                    continue
                seen.add(key)
                if key not in dists_of:
                    dists_of[key] = robust_distributions(legs, s.copula_rho)
                dists = dists_of[key]
            else:
                dists = robust_distributions(legs, s.copula_rho)
            ev = min(_ev(d, payouts[st]) for d in dists)
            if ev <= 0:
                continue
            _, g = _kelly(dists, payouts[st])
            scored.append((legs, st, ev, g))
            if len(scored) > 4 * _KEEP_PER_STRUCTURE:  # brute force: keep memory bounded
                scored = _ranked(scored)[:_KEEP_PER_STRUCTURE]
        if scored:
            out.append(_ranked(scored)[:_KEEP_PER_STRUCTURE])
    return out


def _ranked(entries):
    """Best growth first; ties by the players' names, so the order never depends on input order."""
    return sorted(entries, key=lambda e: (-e[3], sorted(pk.player for pk in e[0])))


def _best_per_structure(picks: list[Pick], s: Settings, allowed, max_brute: int, payouts: Payouts, brute: bool
                        ) -> list[tuple[list[Pick], Structure, float, float]]:
    """The best entry of each allowed structure that shows a profit: (legs, structure, EV, growth)."""
    return [found[0] for found in _scored_per_structure(picks, s, allowed, max_brute, payouts, brute)]


def build_best_entry(picks: list[Pick], s: Settings, allowed=ALLOWED, max_brute: int = 25,
                     payouts: Payouts = PAYOUTS, brute: bool = False
                     ) -> tuple[list[Pick], Structure, float, float] | None:
    """Best entry by full-Kelly growth: (legs, structure, EV, growth). Up to s.pp_max_per_game
    picks per game; EV and growth are the worst case over the same-game correlation (copula_rho
    with either sign, or none). Per structure, the greedy sets (exact under independent picks) and a
    shortlist of the strongest valid sets are scored; brute=True enumerates combinations instead."""
    found = _best_per_structure(picks, s, allowed, max_brute, payouts, brute)
    return max(found, key=lambda e: e[3]) if found else None


def best_by_size(picks: list[Pick], s: Settings, allowed=ALLOWED, max_brute: int = 25,
                 payouts: Payouts = PAYOUTS, brute: bool = False
                 ) -> list[tuple[list[Pick], Structure, float, float]]:
    """The best entry for each number of picks (2 to 6), ranked by full-Kelly growth, best first."""
    by_n: dict[int, tuple[list[Pick], Structure, float, float]] = {}
    for e in _best_per_structure(picks, s, allowed, max_brute, payouts, brute):
        n = e[1][0]
        if n not in by_n or e[3] > by_n[n][3]:
            by_n[n] = e
    return sorted(by_n.values(), key=lambda e: -e[3])


def top_entries(picks: list[Pick], s: Settings, allowed=ALLOWED, max_brute: int = 25,
                payouts: Payouts = PAYOUTS, brute: bool = False, per_size: int = 3, limit: int = 10
                ) -> list[tuple[list[Pick], Structure, float, float]]:
    """Several good entries, ranked by full-Kelly growth, best first: (legs, structure, EV, growth).

    The same search and scoring as build_best_entry, so the first entry is build_best_entry's. Each
    pick set appears once, with its best structure. Up to per_size entries per number of picks; with
    4 or more picks, two entries of the same size differ in at least 2 picks, so they are real
    alternatives rather than one swapped pick.
    """
    best_set: dict[frozenset, tuple[list[Pick], Structure, float, float]] = {}
    for found in _scored_per_structure(picks, s, allowed, max_brute, payouts, brute):
        for e in found:
            key = frozenset(e[0])
            if key not in best_set or e[3] > best_set[key][3]:
                best_set[key] = e
    kept: list[tuple[list[Pick], Structure, float, float]] = []
    for e in _ranked(best_set.values()):
        n = len(e[0])
        same = [k for k in kept if len(k[0]) == n]
        if len(same) >= per_size:
            continue
        if n >= 4 and any(len(set(e[0]) & set(k[0])) > n - 2 for k in same):
            continue
        kept.append(e)
        if len(kept) == limit:
            break
    return kept


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
