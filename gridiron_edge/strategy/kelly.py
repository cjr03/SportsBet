"""Kelly sizing: single bets, pushes, and a joint slate optimizer with caps."""
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm

_ENUM_LIMIT = 32_768  # exact enumeration up to 2^15 win/loss (or 3^9 win/push/loss) scenarios
_MC_DRAWS = 100_000
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
    loss: float = -1.0  # return per unit staked on a loss (a middle only loses part of its cost)


def _kelly_bet(b: Bet) -> float:
    """Single-bet full Kelly with a push and a partial loss: (pw*a - pl*l) / (a*l*(pw+pl))."""
    a, l = b.d - 1.0, -b.loss
    pl = max(0.0, 1.0 - b.p_win - b.p_push)
    if a <= 0 or l <= 0 or b.p_win + pl <= 0:
        return 0.0
    return max(0.0, min((b.p_win * a - pl * l) / (a * l * (b.p_win + pl)), 1.0 / l))


def _corr_matrix(corr, n: int) -> np.ndarray | None:
    """A positive semi-definite correlation matrix, or None when it is the identity."""
    if corr is None:
        return None
    c = np.asarray(corr, dtype=float)
    if c.shape != (n, n) or np.allclose(c, np.eye(n)):
        return None
    vals, vecs = np.linalg.eigh((c + c.T) / 2)
    c = (vecs * np.clip(vals, 1e-9, None)) @ vecs.T
    d = np.sqrt(np.diag(c))
    return c / np.outer(d, d)


def _scenarios(bets: list[Bet], seed: int, corr=None) -> tuple[np.ndarray, np.ndarray]:
    """Return (returns [S, N], weights [S]) over win/(push)/loss outcomes.

    Win/loss only when no bet can push (2^n exact up to 2^15). Correlated outcomes use a
    Gaussian copula: one latent normal per bet, P(win) preserved, Monte Carlo draws.
    """
    n = len(bets)
    two = all(b.p_push <= 0 for b in bets)
    if two:
        rets = np.array([[b.d - 1.0, b.loss] for b in bets])
        cum = np.array([[b.p_win, 1.0] for b in bets])
    else:
        rets = np.array([[b.d - 1.0, 0.0, b.loss] for b in bets])
        cum = np.array([[b.p_win, b.p_win + b.p_push, 1.0] for b in bets])
    cum = np.clip(cum, 0.0, 1.0)
    probs = np.diff(np.hstack([np.zeros((n, 1)), cum]), axis=1)
    probs = probs / probs.sum(axis=1, keepdims=True)
    k = rets.shape[1]
    idx = np.arange(n)
    c = _corr_matrix(corr, n)
    if c is None and k ** n <= _ENUM_LIMIT:
        combos = np.array(list(itertools.product(range(k), repeat=n)))
        return rets[idx, combos], np.prod(probs[idx, combos], axis=1)
    rng = np.random.default_rng(seed)
    if c is None:
        u = rng.random((_MC_DRAWS, n))
    else:
        u = norm.cdf(rng.standard_normal((_MC_DRAWS, n)) @ np.linalg.cholesky(c + 1e-12 * np.eye(n)).T)
    edges = np.cumsum(probs, axis=1)[:, :-1]  # outcome j when u >= edge j-1 and < edge j
    draws = (u[:, :, None] >= edges[None, :, :]).sum(axis=2)
    return rets[idx, draws], np.full(_MC_DRAWS, 1.0 / _MC_DRAWS)


def _constraints(bets: list[Bet], cap_game: float, cap_slate: float, game_caps, groups
                 ) -> list[tuple[np.ndarray, float]]:
    """(mask, cap) pairs: slate total, each game (its own cap if given), and extra groups."""
    n = len(bets)
    out = [(np.ones(n), cap_slate)]
    games: dict[str, list[int]] = {}
    for i, b in enumerate(bets):
        games.setdefault(b.game_id, []).append(i)
    for gid, idxs in games.items():
        mask = np.zeros(n)
        mask[idxs] = 1.0
        out.append((mask, min(cap_game, (game_caps or {}).get(gid, cap_game))))
    for idxs, cap in groups or []:
        mask = np.zeros(n)
        mask[list(idxs)] = 1.0
        out.append((mask, cap))
    return [(m, max(0.0, c)) for m, c in out]


def _fallback(bets: list[Bet], fraction: float, cap_bet: float, cons, scales) -> list[float]:
    f = [min(_kelly_bet(b) * fraction * s, cap_bet) for b, s in zip(bets, scales)]
    for mask, cap in cons:  # scaling a group down never pushes another group over its cap
        tot = sum(x for x, m in zip(f, mask) if m)
        if tot > cap:
            f = [x * cap / tot if m else x for x, m in zip(f, mask)]
    return f


def optimize_slate(bets: list[Bet], fraction: float, cap_bet: float, cap_game: float,
                   cap_slate: float, seed: int = 0, *, scales: Sequence[float] | None = None,
                   game_caps: dict[str, float] | None = None,
                   groups: Sequence[tuple[Sequence[int], float]] | None = None,
                   corr=None) -> list[float]:
    """Maximize E[log(1 + sum x_i r_i)] under caps; returns bankroll fractions f_i = x_i * fraction * scale_i.

    scales: per-bet Kelly multipliers (uncertainty shrinkage, throttles), default 1.
    game_caps: per-game cap overriding cap_game (remaining headroom after open bets).
    groups: extra (bet indices, cap) limits, e.g. a slate window or same-direction legs.
    corr: bet-outcome correlation matrix for the Gaussian copula (None = independent).
    All caps are fractions of the same base as the returned fractions.
    """
    if not bets:
        return []
    n = len(bets)
    sc = np.array([1.0] * n if scales is None else [max(0.0, float(x)) for x in scales])
    cons = _constraints(bets, cap_game, cap_slate, game_caps, groups)
    try:
        R, w = _scenarios(bets, seed, corr)
        k = fraction * sc  # f = k * x

        def neg(x):
            g = np.maximum(1.0 + R @ x, _EPS)
            return -float(w @ np.log(g))

        def grad(x):
            g = np.maximum(1.0 + R @ x, _EPS)
            return -(R.T @ (w / g))

        scipy_cons = [{"type": "ineq", "fun": lambda x, m=m * k, c=c: c - m @ x, "jac": lambda x, m=m * k: -m}
                      for m, c in cons]
        bounds = [(0.0, cap_bet / ki if ki > 0 else 0.0) for ki in k]
        res = minimize(neg, np.zeros(n), jac=grad, method="SLSQP", bounds=bounds, constraints=scipy_cons)
        if not res.success or not np.all(np.isfinite(res.x)):
            raise RuntimeError(res.message)
        return [float(max(0.0, x)) * ki for x, ki in zip(res.x, k)]
    except Exception:
        return _fallback(bets, fraction, cap_bet, cons, sc)


def to_cents(fractions: list[float], bankroll_cents: int, min_stake_cents: int) -> list[int]:
    """Floor each stake to whole cents; stakes under the minimum become 0."""
    out = []
    for f in fractions:
        c = math.floor(round(f * bankroll_cents, 6))
        out.append(c if c >= min_stake_cents else 0)
    return out


def clv_throttle(clvs: Sequence[float], min_n: int = 10) -> float:
    """Kelly multiplier from recent closing-line value: 1 normally, 0.5 when the mean is below 0
    (t < 0), 0 (observe only) when t < -2. Too few bets: 1."""
    n = len(clvs)
    if n < max(2, min_n):
        return 1.0
    mean = sum(clvs) / n
    sd = math.sqrt(sum((c - mean) ** 2 for c in clvs) / (n - 1))
    t = mean / (sd / math.sqrt(n)) if sd > 0 else (math.copysign(math.inf, mean) if mean else 0.0)
    return 0.0 if t < -2 else 0.5 if t < 0 else 1.0
