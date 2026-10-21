"""NFL ratings: recency-weighted ridge on margins blended with an EPA rating."""
from datetime import date, datetime

import numpy as np
import polars as pl

from gridiron_edge.models import Prediction  # noqa: F401  (shared with cfb)
from gridiron_edge.models.ridge import implied_ratings, weighted_ridge

MODEL_VERSION = "nfl-ridge-v1"
LAMBDA = 8.0
HALF_LIFE_DAYS = 120.0
EPA_SCALE = 65.0
# points / EPA / market-implied (closing spreads of past games) shares, renormalised over the
# components available; without lines this is the original 0.6/0.4 points/EPA blend
W_PTS, W_EPA, W_MKT = 0.45, 0.30, 0.25


def _weight(age_days: np.ndarray) -> np.ndarray:
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def is_neutral(r: dict) -> bool:
    """nflverse marks neutral sites with location == "Neutral" (tests may pass neutral=True)."""
    return bool(r.get("neutral")) or r.get("location") == "Neutral"


class NflRatings:
    def __init__(self, ratings: dict[str, float], hfa: float, pts_for: dict[str, float],
                 pts_against: dict[str, float]):
        self.ratings = ratings
        self.hfa = hfa
        self._pf = pts_for
        self._pa = pts_against

    @classmethod
    def fit(cls, schedules: pl.DataFrame, team_epa: pl.DataFrame | None, asof: date) -> "NflRatings":
        rows = [r for r in schedules.iter_rows(named=True)
                if r["home_score"] is not None and r["away_score"] is not None
                and _as_date(r["gameday"]) < asof]
        teams = sorted({r["home_team"] for r in rows} | {r["away_team"] for r in rows})
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        X = np.zeros((len(rows), n + 1))
        y = np.zeros(len(rows))
        age = np.zeros(len(rows))
        for k, r in enumerate(rows):
            X[k, idx[r["home_team"]]] = 1.0
            X[k, idx[r["away_team"]]] = -1.0
            X[k, n] = 0.0 if is_neutral(r) else 1.0
            y[k] = r["home_score"] - r["away_score"]
            age[k] = (asof - _as_date(r["gameday"])).days
        w = _weight(age)
        penalty = np.concatenate([np.full(n, LAMBDA), [0.0]])
        beta = weighted_ridge(X, y, w, penalty) if rows else np.zeros(n + 1)
        r_pts = beta[:n]
        hfa = float(beta[n])

        r_epa = cls._epa_ratings(schedules, team_epa, asof, idx)
        r_mkt = cls._market_ratings(rows, w, idx)
        parts = [(W_PTS, r_pts)] + [(wt, r) for wt, r in ((W_EPA, r_epa), (W_MKT, r_mkt)) if r is not None]
        blended = sum(wt * r for wt, r in parts) / sum(wt for wt, _ in parts)

        pf, pa = {}, {}
        for t in teams:
            ws = ps = pas = 0.0
            for k, r in enumerate(rows):
                if t == r["home_team"]:
                    f, a = r["home_score"], r["away_score"]
                elif t == r["away_team"]:
                    f, a = r["away_score"], r["home_score"]
                else:
                    continue
                ws += w[k]
                ps += w[k] * f
                pas += w[k] * a
            pf[t], pa[t] = ps / ws, pas / ws
        return cls({t: float(blended[idx[t]]) for t in teams}, hfa, pf, pa)

    @staticmethod
    def _market_ratings(rows: list[dict], w: np.ndarray, idx: dict[str, int]) -> np.ndarray | None:
        """Ratings implied by past games' closing spreads (nflverse spread_line: + = home favoured)."""
        lined = [(r["home_team"], r["away_team"], is_neutral(r), float(r["spread_line"]), float(w[k]))
                 for k, r in enumerate(rows) if r.get("spread_line") is not None]
        if not lined:
            return None
        ratings, _ = implied_ratings(lined, LAMBDA)
        r = np.array([ratings.get(t, 0.0) for t in idx])
        seen = np.array([t in ratings for t in idx])
        return r - r[seen].mean()

    @staticmethod
    def _epa_ratings(schedules, team_epa, asof, idx) -> np.ndarray | None:
        if team_epa is None or team_epa.is_empty():
            return None
        # a week's EPA is only usable once every game of that week was played before asof
        week_end = (schedules.group_by(["season", "week"])
                    .agg(pl.col("gameday").cast(pl.Date).max().alias("_end")))
        df = (team_epa.join(week_end, on=["season", "week"], how="inner")
              .filter(pl.col("_end") < asof)
              .drop_nulls(["off_epa", "def_epa"]))
        if df.is_empty():
            return None
        n = len(idx)
        sums = np.zeros(n)
        wsum = np.zeros(n)
        for r in df.iter_rows(named=True):
            i = idx.get(r["team"])
            if i is None:
                continue
            wt = float(_weight(np.array((asof - r["_end"]).days)))
            sums[i] += wt * (r["off_epa"] - r["def_epa"]) * EPA_SCALE
            wsum[i] += wt
        # per-team ridge toward 0 (intercept-only design): sum(w*y) / (sum(w) + lambda)
        r = sums / (wsum + LAMBDA)
        return r - r[wsum > 0].mean() if (wsum > 0).any() else r

    def predict(self, home: str, away: str, neutral: bool = False) -> float:
        return (self.ratings.get(home, 0.0) - self.ratings.get(away, 0.0)
                + (0.0 if neutral else self.hfa))

    def predict_total(self, home: str, away: str) -> float | None:
        if home not in self._pf or away not in self._pf:
            return None
        return ((self._pf[home] + self._pa[home]) / 2 + (self._pf[away] + self._pa[away]) / 2)
