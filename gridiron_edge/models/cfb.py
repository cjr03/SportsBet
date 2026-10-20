"""CFB ratings: prior-anchored weighted ridge on capped margins."""
from datetime import date, datetime
from typing import Callable

import numpy as np

from gridiron_edge.models.ridge import weighted_ridge

MODEL_VERSION = "cfb-ridge-v1"
MARGIN_CAP = 28.0
WEEKLY_DECAY = 0.97
UNKNOWN_PRIOR = -20.0


def _default_lam(w: float) -> float:
    return max(2.0, 20.0 - 2.0 * w)


def _game_date(g: dict) -> date:
    v = g["startDate"]
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


class CfbRatings:
    def __init__(self, ratings: dict[str, float], hfa: float):
        self.ratings = ratings
        self.hfa = hfa

    @classmethod
    def fit(cls, games: list[dict], priors: dict[str, float], asof: date,
            lam_by_week: Callable[[float], float] = _default_lam) -> "CfbRatings":
        """games use CFBD keys: homeTeam, awayTeam, homePoints, awayPoints, neutralSite, week, startDate."""
        used = [g for g in games
                if g.get("homePoints") is not None and g.get("awayPoints") is not None
                and _game_date(g) < asof]
        teams = sorted({g["homeTeam"] for g in used} | {g["awayTeam"] for g in used} | set(priors))
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        X = np.zeros((len(used), n + 1))
        y = np.zeros(len(used))
        w = np.zeros(len(used))
        for k, g in enumerate(used):
            X[k, idx[g["homeTeam"]]] = 1.0
            X[k, idx[g["awayTeam"]]] = -1.0
            X[k, n] = 0.0 if g.get("neutralSite") else 1.0
            y[k] = float(np.clip(g["homePoints"] - g["awayPoints"], -MARGIN_CAP, MARGIN_CAP))
            w[k] = WEEKLY_DECAY ** ((asof - _game_date(g)).days / 7.0)
        cur_week = max((g.get("week") or 0 for g in used), default=0)
        lam = lam_by_week(cur_week)
        prior = np.array([priors.get(t, UNKNOWN_PRIOR) for t in teams] + [0.0])
        penalty = np.concatenate([np.full(n, lam), [0.0]])
        beta = weighted_ridge(X, y, w, penalty, prior)
        return cls({t: float(beta[idx[t]]) for t in teams}, float(beta[n]))

    def predict(self, home: str, away: str, neutral: bool) -> float:
        return (self.ratings.get(home, UNKNOWN_PRIOR) - self.ratings.get(away, UNKNOWN_PRIOR)
                + (0.0 if neutral else self.hfa))


def cfb_prior(prev_rating: dict[str, float], returning_ppa: dict[str, float],
              talent: dict[str, float]) -> dict[str, float]:
    """R0 = 0.6*P + 0.25*P*(RP-0.5) + 0.15*z(talent)*7; RP defaults to 0.5, z to 0."""
    vals = np.array(list(talent.values()), dtype=float)
    mu = vals.mean() if vals.size else 0.0
    sd = vals.std() if vals.size else 0.0
    out = {}
    for team, p in prev_rating.items():
        rp = returning_ppa.get(team, 0.5)
        z = (talent[team] - mu) / sd if team in talent and sd > 0 else 0.0
        out[team] = 0.6 * p + 0.25 * p * (rp - 0.5) + 0.15 * z * 7
    return out
