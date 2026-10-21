"""Player prop projections from nflverse weekly stats.

mean = usage x efficiency (each recency-weighted, efficiency shrunk to the league rate), or, without
usage columns, the weighted mean shrunk toward a position prior; then an opponent factor and a game
script factor. sd = per-player CV shrunk toward the stat's pooled CV. Games with a low snap share are
dropped from the history, games for a previous team are down-weighted, and players listed Out are
skipped.
"""
import math
from dataclasses import dataclass

import numpy as np
import polars as pl

from gridiron_edge.matching import normalize_name
from gridiron_edge.strategy.distributions import prop_p_over

STAT_COLUMNS = {
    "reception_yds": "receiving_yards",
    "rush_yds": "rushing_yards",
    "pass_yds": "passing_yards",
    "receptions": "receptions",
}
# usage column for each stat column; yards (or catches) per unit of usage is the efficiency
USAGE_COLUMNS = {"receiving_yards": "targets", "rushing_yards": "carries", "passing_yards": "attempts",
                 "receptions": "targets"}
# prior usage weight behind the league efficiency (in targets / carries / attempts)
EFF_PRIOR_USAGE = {"receiving_yards": 25.0, "rushing_yards": 40.0, "passing_yards": 80.0, "receptions": 25.0}
MAX_GAMES = 12
MIN_GAMES = 4
HALF_LIFE = 4.0
SHRINK_K = 3.0            # mean prior weight, in effective games
VAR_PRIOR_DF = 6.0        # variance prior weight, in degrees of freedom
TEAM_CHANGE_WEIGHT = 0.5  # games for a previous team count half
MIN_SNAP_SHARE = 0.5
OPP_GAMES = 16
OPP_PRIOR_GAMES = 8.0
OPP_CLIP = (0.85, 1.15)
# game script, per point of the team's implied total above 22 / per point the team is favoured by
SCRIPT_TOTAL = 0.01
SCRIPT_FAV = {"rushing_yards": 0.008, "passing_yards": -0.004, "receiving_yards": -0.004, "receptions": -0.004}
SCRIPT_CLIP = (0.85, 1.15)
LEAGUE_TEAM_TOTAL = 22.0
_OUT = ("out",)
# projector calibration gate
GATE_MIN_PICKS = 300
GATE_TOLERANCE = 0.01


_GATE_POSITIONS = {"passing_yards": {"QB"}, "rushing_yards": {"RB", "QB"},
                   "receiving_yards": {"WR", "TE", "RB"}}


def _team_col(df: pl.DataFrame) -> str | None:
    return next((c for c in ("recent_team", "team") if c in df.columns), None)


@dataclass
class _Hist:
    """One player's games as arrays, newest first (numpy: polars per-call overhead is too slow here)."""
    season: np.ndarray
    week: np.ndarray
    team: np.ndarray | None
    position: str | None
    snap: np.ndarray | None
    cols: dict[str, np.ndarray]


def _histories(df: pl.DataFrame) -> dict[str, _Hist]:
    if df.height == 0:
        return {}
    numeric = [c for c in {*STAT_COLUMNS.values(), *USAGE_COLUMNS.values()} if c in df.columns]
    tcol = _team_col(df)
    df = df.sort(["_key", "season", "week"], descending=[False, True, True])
    keys = df["_key"].to_numpy()
    season, week = df["season"].to_numpy().astype(int), df["week"].to_numpy().astype(int)
    team = df[tcol].to_numpy() if tcol else None
    pos = df["position"].to_list() if "position" in df.columns else None
    snap = df["_snap"].cast(pl.Float64).fill_null(np.nan).to_numpy() if "_snap" in df.columns else None
    cols = {c: df[c].cast(pl.Float64).fill_null(np.nan).to_numpy() for c in numeric}
    out = {}
    bounds = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1], True])
    for a, b in zip(bounds[:-1], bounds[1:]):
        if keys[a] is None:
            continue
        sl = slice(a, b)
        out[keys[a]] = _Hist(season[sl], week[sl], None if team is None else team[sl],
                             None if pos is None else next((p for p in pos[sl] if p), None),
                             None if snap is None else snap[sl], {c: v[sl] for c, v in cols.items()})
    return out


class PropProjector:
    def __init__(self, weekly: pl.DataFrame, snaps: pl.DataFrame | None = None,
                 injuries: pl.DataFrame | None = None, usage_model: bool = True):
        df = weekly.with_columns(
            pl.col("player_display_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("_key"))
        if snaps is not None and {"player", "season", "week", "offense_pct"} <= set(snaps.columns):
            share = (snaps.select(pl.col("player").map_elements(normalize_name, return_dtype=pl.Utf8).alias("_key"),
                                  pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64),
                                  pl.col("offense_pct").cast(pl.Float64).alias("_snap"))
                     .group_by(["_key", "season", "week"]).agg(pl.col("_snap").max()))
            df = df.with_columns(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)).join(
                share, on=["_key", "season", "week"], how="left")
            # snap share may be a fraction or a percentage
            df = df.with_columns(pl.when(pl.col("_snap") > 1).then(pl.col("_snap") / 100)
                                 .otherwise(pl.col("_snap")).alias("_snap"))
        self._weekly = df
        self._hist = _histories(df)
        self._out: set[tuple[str, int, int]] = set()
        if injuries is not None and {"full_name", "season", "week", "report_status"} <= set(injuries.columns):
            for name, season, week, status in injuries.select("full_name", "season", "week", "report_status").iter_rows():
                if name and status and status.strip().lower() in _OUT:
                    self._out.add((normalize_name(name), int(season), int(week)))
        self.usage_model = usage_model
        self.unmatched: set[str] = set()
        self._cache: dict[tuple, float | None] = {}

    # -- priors ------------------------------------------------------------
    def _history(self, season: int) -> pl.DataFrame:
        df = self._weekly.filter(pl.col("season") < season)
        if "_snap" in df.columns:
            df = df.filter(pl.col("_snap").is_null() | (pl.col("_snap") >= MIN_SNAP_SHARE))
        return df

    def _pooled_cv(self, col: str, season: int) -> float | None:
        """Median per-player CV of the stat across regular players in past seasons."""
        key = ("cv", col, season)
        if key not in self._cache:
            g = (self._history(season).drop_nulls(col).group_by("_key")
                 .agg(pl.col(col).mean().alias("m"), pl.col(col).std().alias("s"), pl.len().alias("n"))
                 .filter((pl.col("n") >= MIN_GAMES) & (pl.col("m") > 0)))
            g = g.filter(pl.col("m") >= g["m"].median()) if g.height else g  # regulars, not backups
            self._cache[key] = float((g["s"] / g["m"]).median()) if g.height >= 5 else None
        return self._cache[key]

    def _position_prior(self, col: str, position: str | None, season: int) -> float | None:
        """Mean of per-player means at the position, over regulars in past seasons."""
        if position is None or "position" not in self._weekly.columns:
            return None
        key = ("pos", col, position, season)
        if key not in self._cache:
            g = (self._history(season).filter(pl.col("position") == position).drop_nulls(col)
                 .group_by("_key").agg(pl.col(col).mean().alias("m"), pl.len().alias("n"))
                 .filter(pl.col("n") >= MIN_GAMES))
            self._cache[key] = float(g["m"].mean()) if g.height >= 5 else None
        return self._cache[key]

    def _league_eff(self, col: str, season: int) -> float | None:
        ucol = USAGE_COLUMNS.get(col)
        if ucol is None or ucol not in self._weekly.columns:
            return None
        key = ("eff", col, season)
        if key not in self._cache:
            h = self._history(season).drop_nulls([col, ucol])
            u = float(h[ucol].sum()) if h.height else 0.0
            self._cache[key] = float(h[col].sum()) / u if u > 0 else None
        return self._cache[key]

    def opponent_factor(self, col: str, opponent: str, season: int, asof_week: int) -> float:
        """How much of the stat this defense allows per game vs the league, shrunk and clipped."""
        df = self._weekly
        if "opponent_team" not in df.columns:
            return 1.0
        key = ("opp", col, opponent, season, asof_week)
        if key not in self._cache:
            self._cache[key] = self._opponent_factor(df, col, opponent, season, asof_week)
        return self._cache[key]

    @staticmethod
    def _opponent_factor(df: pl.DataFrame, col: str, opponent: str, season: int, asof_week: int) -> float:
        before = df.filter((pl.col("season") < season) | ((pl.col("season") == season) & (pl.col("week") < asof_week)))
        per_game = before.drop_nulls(col).group_by(["opponent_team", "season", "week"]).agg(pl.col(col).sum())
        if per_game.height == 0:
            return 1.0
        league = float(per_game[col].mean())
        mine = per_game.filter(pl.col("opponent_team") == opponent).sort(["season", "week"], descending=True).head(OPP_GAMES)
        if league <= 0 or mine.height == 0:
            return 1.0
        f = (float(mine[col].sum()) + OPP_PRIOR_GAMES * league) / ((mine.height + OPP_PRIOR_GAMES) * league)
        return float(np.clip(f, *OPP_CLIP))

    @staticmethod
    def script_factor(col: str, team_total: float | None, team_spread: float | None) -> float:
        """Volume from the market: a higher team total means more of everything; a favourite (negative
        spread) runs more and throws less."""
        f = 1.0
        if team_total is not None:
            f += SCRIPT_TOTAL * (team_total - LEAGUE_TEAM_TOTAL)
        if team_spread is not None:
            f += SCRIPT_FAV.get(col, 0.0) * -team_spread
        return float(np.clip(f, *SCRIPT_CLIP))

    # -- projection ----------------------------------------------------------
    def is_out(self, player: str, season: int, week: int) -> bool:
        return (normalize_name(player), season, week) in self._out

    def _window(self, h: "_Hist", col: str, season: int, asof_week: int) -> np.ndarray:
        """Indices (newest first) of usable games before asof_week, at most MAX_GAMES."""
        vals = h.cols[col]
        mask = ((h.season < season) | ((h.season == season) & (h.week < asof_week))) & ~np.isnan(vals)
        if h.snap is not None:  # games where he barely played say little about a full game
            mask &= np.isnan(h.snap) | (h.snap >= MIN_SNAP_SHARE)
        return np.flatnonzero(mask)[:MAX_GAMES]

    def project(self, player: str, stat: str, season: int, asof_week: int, *, opponent: str | None = None,
                team_total: float | None = None, team_spread: float | None = None) -> tuple[float, float] | None:
        """(mean, sd) of the stat for the player's game in asof_week, or None."""
        col = STAT_COLUMNS.get(stat)
        if col is None or col not in self._weekly.columns:
            return None
        key = normalize_name(player)
        h = self._hist.get(key)
        if h is None:
            self.unmatched.add(player)
            return None
        if (key, season, asof_week) in self._out:
            return None
        idx = self._window(h, col, season, asof_week)
        if len(idx) < MIN_GAMES:
            return None
        vals = h.cols[col][idx]  # newest first
        w = 0.5 ** (np.arange(len(vals)) / HALF_LIFE)
        if h.team is not None:
            teams = h.team[idx]
            w = w * np.where(teams == teams[0], 1.0, TEAM_CHANGE_WEIGHT)
        n_eff = w.sum() ** 2 / (w ** 2).sum()
        raw = float(np.average(vals, weights=w))
        mean = self._mean(h, idx, col, vals, w, n_eff, raw, season)
        if opponent is not None:
            mean *= self.opponent_factor(col, opponent, season, asof_week)
        mean *= self.script_factor(col, team_total, team_spread)
        # weighted variance with the effective-sample correction, CV shrunk toward the pooled CV
        var = float(np.average((vals - raw) ** 2, weights=w)) * n_eff / max(n_eff - 1, 1e-9)
        cv_player = math.sqrt(var) / raw if raw > 0 else None
        pooled = self._pooled_cv(col, season)
        if cv_player is None:
            cv = pooled
        elif pooled is None:
            cv = cv_player
        else:
            df_p = n_eff - 1
            cv = math.sqrt((df_p * cv_player ** 2 + VAR_PRIOR_DF * pooled ** 2) / (df_p + VAR_PRIOR_DF))
        if cv is None or mean <= 0:
            return None
        return mean, cv * mean

    def _mean(self, h: "_Hist", idx: np.ndarray, col: str, vals: np.ndarray, w: np.ndarray, n_eff: float,
              raw: float, season: int) -> float:
        ucol = USAGE_COLUMNS.get(col)
        eff0 = self._league_eff(col, season) if self.usage_model else None
        u = h.cols[ucol][idx] if ucol in h.cols else None
        if eff0 is not None and u is not None and not np.isnan(u).any():
            usage = float(np.average(u, weights=w))
            k = EFF_PRIOR_USAGE[col]
            eff = (float((w * vals).sum()) + k * eff0) / (float((w * u).sum()) + k)
            return usage * eff
        prior = self._position_prior(col, h.position, season)
        if prior is None:
            return raw
        return (n_eff * raw + SHRINK_K * prior) / (n_eff + SHRINK_K)

    def team_of(self, player: str) -> str | None:
        """The player's latest team from the weekly stats."""
        h = self._hist.get(normalize_name(player))
        if h is None or h.team is None:
            return None
        return next((t for t in h.team if t is not None), None)

    # -- gate ----------------------------------------------------------------
    def calibration_backtest(self, season: int, margin: float, max_games: int = 4000) -> dict:
        """Outcome-only check of the projector on a past season (no odds history needed).

        Each player-game is priced at a naive line (his trailing 8-game median, x.5); where the
        projector claims at least 0.5 + margin on a side, we compare its claimed chance with the hit
        rate. One line per player-game, so picks are roughly independent. live = enough picks and the
        realised rate's 95% lower bound within GATE_TOLERANCE of the claimed rate.
        """
        claimed, hit = [], []
        stats = [s for s, c in STAT_COLUMNS.items() if c in self._weekly.columns and s != "receptions"]
        games = sorted(((int(h.week[i]), key, i) for key, h in self._hist.items()
                        for i in np.flatnonzero((h.season == season) & (h.week >= 5))),
                       key=lambda t: t[0], reverse=True)
        for week, key, i in games[:max_games]:
            h = self._hist[key]
            for stat in stats:
                col = STAT_COLUMNS[stat]
                if h.position is not None and h.position not in _GATE_POSITIONS[col]:
                    continue
                actual = h.cols[col][i]
                past = self._window(h, col, season, week)[:8]
                if np.isnan(actual) or len(past) < MIN_GAMES or np.median(h.cols[col][past]) < 10:
                    continue
                line = math.floor(float(np.median(h.cols[col][past]))) + 0.5
                proj = self.project(key, stat, season, week)
                if proj is None:
                    continue
                p = prop_p_over(line, proj[0], "yards", proj[1])
                side_p, won = (p, actual > line) if p >= 0.5 else (1 - p, actual < line)
                if side_p >= 0.5 + margin:
                    claimed.append(side_p)
                    hit.append(float(won))
        n = len(hit)
        if n == 0:
            return {"n": 0, "claimed": 0.0, "hit": 0.0, "live": False}
        rate, c = float(np.mean(hit)), float(np.mean(claimed))
        lower = rate - 1.645 * math.sqrt(max(rate * (1 - rate), 1e-9) / n)
        return {"n": n, "claimed": c, "hit": rate,
                "live": n >= GATE_MIN_PICKS and lower >= c - GATE_TOLERANCE}
