"""Closed-form weighted ridge shared by the rating models."""
import numpy as np


def weighted_ridge(X: np.ndarray, y: np.ndarray, w: np.ndarray, penalty: np.ndarray,
                   prior: np.ndarray | None = None) -> np.ndarray:
    """Solve (X'WX + diag(penalty)) b = X'Wy + penalty*prior; a zero penalty leaves a column free."""
    prior = np.zeros(X.shape[1]) if prior is None else prior
    XtW = X.T * w
    A = XtW @ X + np.diag(penalty)
    return np.linalg.solve(A, XtW @ y + penalty * prior)
