"""nflverse data via nflreadpy, with a direct-parquet fallback. Synchronous: call from threads."""
from pathlib import Path

import nflreadpy
import polars as pl

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE_COLS = ["game_id", "season", "week", "gameday", "home_team", "away_team",
                 "home_score", "away_score", "result", "spread_line", "total_line",
                 "location", "home_qb_id", "away_qb_id"]  # location "Neutral" = no home field
_RUSH_PASS = ("pass", "run")


def qb1_from_depth_chart(df: pl.DataFrame) -> dict[str, str]:
    """Each team's latest listed starting QB (gsis id) from an nflverse depth chart.

    Handles the 2025+ daily schema (dt, team, pos_abb, pos_rank) and the older weekly one
    (season, week, club_code, position, depth_team).
    """
    if df.is_empty() or "gsis_id" not in df.columns:
        return {}
    if {"dt", "team", "pos_abb", "pos_rank"} <= set(df.columns):
        qbs = df.filter((pl.col("pos_abb") == "QB") & (pl.col("pos_rank") == 1))
        team, order = "team", ["dt"]
    elif {"club_code", "position", "depth_team", "week"} <= set(df.columns):
        qbs = df.filter((pl.col("position") == "QB") & (pl.col("depth_team").cast(pl.String) == "1"))
        team, order = "club_code", [c for c in ("season", "week") if c in df.columns]
    else:
        return {}
    qbs = qbs.drop_nulls([team, "gsis_id"]).sort(order)
    return {t: q for t, q in qbs.group_by(team, maintain_order=True).agg(pl.col("gsis_id").last()).iter_rows()}


class NflverseSource:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _load(self, loader: str, tag: str, file_fmt: str, seasons: list[int]) -> pl.DataFrame:
        try:
            return getattr(nflreadpy, loader)(seasons)
        except Exception:  # nflreadpy failed (network, upstream format): read the release assets directly
            urls = list(dict.fromkeys(  # constant file names (schedules) collapse to one download
                f"{RELEASES}/{tag}/{file_fmt.format(season=s)}.parquet" for s in seasons))
            df = pl.concat([pl.read_parquet(u) for u in urls], how="diagonal_relaxed")
            return df.filter(pl.col("season").is_in(seasons)) if "season" in df.columns else df

    def schedules(self, seasons: list[int]) -> pl.DataFrame:
        df = self._load("load_schedules", "schedules", "games", seasons)
        return df.select([c for c in SCHEDULE_COLS if c in df.columns])

    def weekly_player_stats(self, seasons: list[int]) -> pl.DataFrame:
        return self._load("load_player_stats", "player_stats", "player_stats_{season}", seasons)

    def snap_counts(self, seasons: list[int]) -> pl.DataFrame:
        return self._load("load_snap_counts", "snap_counts", "snap_counts_{season}", seasons)

    def injuries(self, seasons: list[int]) -> pl.DataFrame:
        return self._load("load_injuries", "injuries", "injuries_{season}", seasons)

    def team_epa(self, seasons: list[int]) -> pl.DataFrame:
        pbp = self._load("load_pbp", "pbp", "play_by_play_{season}", seasons)
        plays = pbp.filter(pl.col("epa").is_not_null() & pl.col("play_type").is_in(_RUSH_PASS))
        keys = ["season", "week"]
        off = plays.group_by([*keys, "game_id", pl.col("posteam").alias("team")]).agg(
            pl.col("epa").mean().alias("off_epa"), pl.len().alias("plays"))
        deff = plays.group_by([*keys, "game_id", pl.col("defteam").alias("team")]).agg(
            pl.col("epa").mean().alias("def_epa"))
        return (off.join(deff, on=[*keys, "game_id", "team"], how="full", coalesce=True)
                .select("season", "week", "team", "off_epa", "def_epa", "plays"))

    def depth_chart_qb1(self, season: int) -> dict[str, str]:
        """Latest depth-chart QB1 per team (free nflverse release; feeds the QB-change veto)."""
        return qb1_from_depth_chart(self._load("load_depth_charts", "depth_charts", "depth_charts_{season}", [season]))
