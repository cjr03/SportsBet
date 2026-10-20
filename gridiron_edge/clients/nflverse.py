"""nflverse data via nflreadpy, with a direct-parquet fallback. Synchronous: call from threads."""
from pathlib import Path

import nflreadpy
import polars as pl

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE_COLS = ["game_id", "season", "week", "gameday", "home_team", "away_team",
                 "home_score", "away_score", "result", "spread_line", "total_line"]
_RUSH_PASS = ("pass", "run")


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
