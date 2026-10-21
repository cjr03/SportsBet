"""Logit blend of model and market probabilities, with an auto-gate and a walk-forward ATS backtest."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import numpy as np

from gridiron_edge.strategy.distributions import CFB_SIGMA, NFL_SPREAD_SIGMA, NormalModel

MIN_FIT_N = 200
GATE_Z = 1.96
_EPS = 1e-6
BREAKEVEN_110 = 110 / 210  # win rate needed at -110
RELIABILITY_BINS = 5


def _logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), _EPS, 1 - _EPS)
    return np.log(p / (1 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


@dataclass
class BlendFit:
    a: float
    b_model: float
    b_market: float
    b_model_se: float
    n: int

    @property
    def gated(self) -> bool:
        """True when the model has not earned any weight over the market."""
        if self.n < MIN_FIT_N or self.b_model <= 0 or self.b_model_se <= 0:
            return True
        return self.b_model / self.b_model_se < GATE_Z


def fit_blend(p_model, p_market, y) -> BlendFit:
    """Logistic regression of y on [1, logit p_model, logit p_market] (Newton-Raphson).

    p_market=None drops the market column (the ATS backtest's market is a constant 0.5); b_market is 0.
    """
    y = np.asarray(y, dtype=float)
    cols = [np.ones(len(y)), _logit(p_model)] + ([] if p_market is None else [_logit(p_market)])
    X = np.column_stack(cols)
    k = X.shape[1]
    beta = np.zeros(k)
    ridge = 1e-9 * np.eye(k)  # keeps the Hessian invertible if a column is constant
    for _ in range(50):
        p = _sigmoid(X @ beta)
        grad = X.T @ (y - p)
        hess = (X * (p * (1 - p))[:, None]).T @ X + ridge
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    p = _sigmoid(X @ beta)
    hess = (X * (p * (1 - p))[:, None]).T @ X + ridge
    se = np.sqrt(np.diag(np.linalg.inv(hess)))
    return BlendFit(float(beta[0]), float(beta[1]), float(beta[2]) if k > 2 else 0.0, float(se[1]), len(y))


def blend(p_model: float, p_market: float, w_model: float) -> float:
    z = w_model * _logit(p_model) + (1 - w_model) * _logit(p_market)
    return float(_sigmoid(z))


def default_weight(league: str, hours_to_kickoff: float) -> float:
    if hours_to_kickoff <= 24:
        return 0.125
    return 0.25 if league == "nfl" else 0.35


def walkforward_ats(
    league: str,
    games: list[dict],
    predict_asof: Callable[[date], Callable[[dict], float]],
) -> tuple[BlendFit, dict]:
    """Predict P(home covers) week by week using only data before each week.

    Market probability is 0.5 (the closing line is the reference), so the fit has no market column.
    Pushes are excluded, as are games the predictor cannot forecast yet (it returns None).
    """
    sigma = NFL_SPREAD_SIGMA if league == "nfl" else CFB_SIGMA
    nm = NormalModel(sigma)
    by_week: dict[date, list[dict]] = defaultdict(list)
    for g in games:
        d = g["date"]
        by_week[d - timedelta(days=d.weekday())].append(g)

    p_model, y = [], []
    skipped = 0
    for week_start in sorted(by_week):
        predict = predict_asof(week_start)
        for g in by_week[week_start]:
            cover_margin = -g["home_line"]
            if g["margin"] == cover_margin:
                continue
            mean = predict(g)
            if mean is None:
                skipped += 1
                continue
            p_model.append(nm.p_greater(mean, cover_margin))
            y.append(1.0 if g["margin"] > cover_margin else 0.0)

    pm = np.array(p_model)
    ya = np.array(y)
    fit = fit_blend(pm, None, ya)
    base = {"brier_baseline": 0.25, "logloss_baseline": float(np.log(2)), "breakeven": BREAKEVEN_110,
            "skipped_burn_in": skipped}
    if len(ya) == 0:
        return fit, {"n": 0, "ats_pct": 0.0, "brier": 0.0, "logloss": 0.0, "bss": 0.0,
                     "reliability": reliability([], []), **base}
    pc = np.clip(pm, _EPS, 1 - _EPS)
    picks_home = pm > 0.5
    wins = np.where(picks_home, ya == 1, ya == 0)
    metrics = {
        "n": len(ya),
        "ats_pct": float(wins.mean()),
        "brier": float(np.mean((pm - ya) ** 2)),
        "logloss": float(-np.mean(ya * np.log(pc) + (1 - ya) * np.log(1 - pc))),
        **base,
    }
    metrics["bss"] = 1 - metrics["brier"] / 0.25
    metrics["reliability"] = reliability(pm, ya)
    return fit, metrics


def reliability(p, y, bins: int = RELIABILITY_BINS) -> list[dict]:
    """Equal-width bins on [0, 1]: count, mean forecast and observed hit rate per bin."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    idx = np.minimum((p * bins).astype(int), bins - 1) if len(p) else np.array([], dtype=int)
    out = []
    for b in range(bins):
        mask = idx == b
        n = int(mask.sum())
        out.append({"lo": b / bins, "hi": (b + 1) / bins, "n": n,
                    "mean_p": float(p[mask].mean()) if n else None,
                    "hit_rate": float(y[mask].mean()) if n else None})
    return out
