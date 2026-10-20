"""Kelly sizing: single bets, pushes, and a joint slate optimizer with caps."""
import itertools
import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

_ENUM_LIMIT = 20_000
_EPS = 1e-12


def kelly_binary(p: float, d: float) -> float:
    """Full-Kelly fraction for win prob p at decimal odds d, floored at 0."""
    b = d - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, (p * b - (1.0 - p)) / b)


def kelly_push(pw: float, pl: float, d: float) -> float:
    """Full-Kelly with a push outcome: (pw*b - pl) / (b*(pw+pl)), floored at 0."""
    b = d - 1.0
    if b <= 0 or pw + pl <= 0:
        return 0.0
    return max(0.0, (pw * b - pl) / (b * (pw + pl)))


@dataclass
class Bet:
    p_win: float
    p_push: float
    d: float
    game_id: str


def _scenarios(bets: list[Bet], seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (returns [S, N], weights [S]) over win/push/loss outcomes."""
    n = len(bets)
    rets = np.array([[b.d - 1.0, 0.0, -1.0] for b in bets])
    probs = np.array([[b.p_win, b.p_push, max(0.0, 1.0 - b.p_win - b.p_push)] for b in bets])
    probs = probs / probs.sum(axis=1, keepdims=True)
    if 3 ** n <= _ENUM_LIMIT:
        combos = np.array(list(itertools.product(range(3), repeat=n)))
        idx = np.arange(n)
        w = np.prod(probs[idx, combos], axis=1)
        return rets[idx, combos], w
    rng = np.random.default_rng(seed)
    draws = np.empty((_ENUM_LIMIT, n), dtype=int)
    for i in range(n):
        draws[:, i] = rng.choice(3, size=_ENUM_LIMIT, p=probs[i])
    return rets[np.arange(n), draws], np.full(_ENUM_LIMIT, 1.0 / _ENUM_LIMIT)


def _fallback(bets: list[Bet], fraction: float, cap_bet: float, cap_game: float, cap_slate: float) -> list[float]:
    f = [min(kelly_push(b.p_win, max(0.0, 1.0 - b.p_win - b.p_push), b.d) * fraction, cap_bet)
         for b in bets]
    games: dict[str, list[int]] = {}
    for i, b in enumerate(bets):
        games.setdefault(b.game_id, []).append(i)
    for idxs in games.values():
        tot = sum(f[i] for i in idxs)
        if tot > cap_game:
            for i in idxs:
                f[i] *= cap_game / tot
    tot = sum(f)
    if tot > cap_slate:
        f = [x * cap_slate / tot for x in f]
    return f


def optimize_slate(bets: list[Bet], fraction: float, cap_bet: float, cap_game: float,
                   cap_slate: float, seed: int = 0) -> list[float]:
    """Maximize E[log(1 + sum f_i r_i)] under caps; returns bankroll fractions (x fraction)."""
    if not bets:
        return []
    n = len(bets)
    try:
        R, w = _scenarios(bets, seed)

        def neg(f):
            g = np.maximum(1.0 + R @ f, _EPS)
            return -float(w @ np.log(g))

        def grad(f):
            g = np.maximum(1.0 + R @ f, _EPS)
            return -(R.T @ (w / g))

        games: dict[str, list[int]] = {}
        for i, b in enumerate(bets):
            games.setdefault(b.game_id, []).append(i)
        cons = [{"type": "ineq", "fun": lambda f: cap_slate / fraction - f.sum(),
                 "jac": lambda f: -np.ones(n)}]
        for idxs in games.values():
            mask = np.zeros(n)
            mask[idxs] = 1.0
            cons.append({"type": "ineq", "fun": lambda f, m=mask: cap_game / fraction - m @ f,
                         "jac": lambda f, m=mask: -m})
        res = minimize(neg, np.zeros(n), jac=grad, method="SLSQP",
                       bounds=[(0.0, cap_bet / fraction)] * n, constraints=cons)
        if not res.success or not np.all(np.isfinite(res.x)):
            raise RuntimeError(res.message)
        return [float(max(0.0, x)) * fraction for x in res.x]
    except Exception:
        return _fallback(bets, fraction, cap_bet, cap_game, cap_slate)


def to_cents(fractions: list[float], bankroll_cents: int, min_stake_cents: int) -> list[int]:
    """Floor each stake to whole cents; stakes under the minimum become 0."""
    out = []
    for f in fractions:
        c = math.floor(round(f * bankroll_cents, 6))
        out.append(c if c >= min_stake_cents else 0)
    return out
