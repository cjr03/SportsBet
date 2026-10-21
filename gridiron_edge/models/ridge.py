"""Closed-form weighted ridge shared by the rating models."""
import numpy as np


def weighted_ridge(X: np.ndarray, y: np.ndarray, w: np.ndarray, penalty: np.ndarray,
                   prior: np.ndarray | None = None) -> np.ndarray:
    """Solve (X'WX + diag(penalty)) b = X'Wy + penalty*prior; a zero penalty leaves a column free."""
    prior = np.zeros(X.shape[1]) if prior is None else prior
    XtW = X.T * w
    A = XtW @ X + np.diag(penalty)
    return np.linalg.solve(A, XtW @ y + penalty * prior)


def implied_ratings(rows: list[tuple[str, str, bool, float, float]],
                    lam: float = 1.0) -> tuple[dict[str, float], float]:
    """Market-implied ("implied SRS") ratings: ridge on closing-line home margins.

    rows are (home, away, neutral, implied home margin, weight); returns (ratings, hfa).
    """
    if not rows:
        return {}, 0.0
    teams = sorted({r[0] for r in rows} | {r[1] for r in rows})
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    X = np.zeros((len(rows), n + 1))
    for k, (h, a, neutral, _, _) in enumerate(rows):
        X[k, idx[h]], X[k, idx[a]], X[k, n] = 1.0, -1.0, 0.0 if neutral else 1.0
    y = np.array([r[3] for r in rows], dtype=float)
    w = np.array([r[4] for r in rows], dtype=float)
    # a tiny penalty on hfa keeps an all-neutral slate solvable
    beta = weighted_ridge(X, y, w, np.concatenate([np.full(n, lam), [1e-6]]))
    return {t: float(beta[idx[t]]) for t in teams}, float(beta[n])
