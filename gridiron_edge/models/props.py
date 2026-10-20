"""Player prop projections from nflverse weekly stats."""
import numpy as np
import polars as pl

STAT_COLUMNS = {
    "reception_yds": "receiving_yards",
    "rush_yds": "rushing_yards",
    "pass_yds": "passing_yards",
    "receptions": "receptions",
}
MAX_GAMES = 12
MIN_GAMES = 4
HALF_LIFE = 4.0


class PropProjector:
    def __init__(self, weekly: pl.DataFrame):
        self._weekly = weekly.with_columns(
            pl.col("player_display_name").str.to_lowercase().alias("_key"))

    def project(self, player: str, stat: str, season: int, asof_week: int) -> tuple[float, float] | None:
        col = STAT_COLUMNS.get(stat)
        if col is None or col not in self._weekly.columns:
            return None
        df = (self._weekly
              .filter((pl.col("_key") == player.lower())
                      & ((pl.col("season") < season)
                         | ((pl.col("season") == season) & (pl.col("week") < asof_week)))
                      & pl.col(col).is_not_null())
              .sort(["season", "week"], descending=True)
              .head(MAX_GAMES))
        if df.height < MIN_GAMES:
            return None
        vals = df[col].to_numpy().astype(float)  # newest first
        w = 0.5 ** (np.arange(len(vals)) / HALF_LIFE)
        mean = float(np.average(vals, weights=w))
        sd = float(np.sqrt(np.average((vals - mean) ** 2, weights=w)))
        return mean, sd

    def team_of(self, player: str) -> str | None:
        """The player's latest team from the weekly stats."""
        col = next((c for c in ("recent_team", "team") if c in self._weekly.columns), None)
        if col is None:
            return None
        df = (self._weekly.filter((pl.col("_key") == player.lower()) & pl.col(col).is_not_null())
              .sort(["season", "week"], descending=True).head(1))
        return df[col][0] if df.height else None
