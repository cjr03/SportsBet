"""Every player line we pull, kept with its first and last pre-kickoff price, and graded once played.

The odds cache keeps only the latest pull, so without this log there is no prop history to test
the projector against. grade() answers the one question that matters: does the projector know
anything the closing price does not?
"""
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl

from gridiron_edge.db import register_schema
from gridiron_edge.matching import normalize_name
from gridiron_edge.models.blend import fit_blend
from gridiron_edge.models.props import STAT_COLUMNS
from gridiron_edge.strategy.devig import devig
from gridiron_edge.strategy.distributions import prop_p_over
from gridiron_edge.types import PropQuote

register_schema(
    """CREATE TABLE IF NOT EXISTS prop_lines (
        game_id TEXT NOT NULL, player TEXT NOT NULL, stat TEXT NOT NULL, book TEXT NOT NULL,
        side TEXT NOT NULL, point REAL NOT NULL,
        open_dec REAL, open_at TEXT NOT NULL, close_dec REAL, close_at TEXT NOT NULL,
        PRIMARY KEY (game_id, player, stat, book, side, point))""")

MIN_GRADED = 300  # fewer graded lines than this: too few to say anything
EDGE_MIN = 0.03  # the projector "disagrees" when its P(side) beats the closing price's by this much
PLAYED_AFTER = timedelta(hours=12)  # a game this long past kickoff is played (its status may lag)


def record(conn: sqlite3.Connection, props: list[PropQuote], kickoffs: dict[str, datetime], now: datetime) -> int:
    """Log lines for games that have not kicked off: the first sighting is the open, the latest the close."""
    at = now.astimezone(UTC).isoformat()
    rows = [(q.game_id, q.player, q.stat, q.book, q.side, float(q.point), q.decimal, at, q.decimal, at)
            for q in props if q.game_id in kickoffs and now < kickoffs[q.game_id]]
    conn.executemany(
        "INSERT INTO prop_lines (game_id, player, stat, book, side, point, open_dec, open_at, close_dec, close_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (game_id, player, stat, book, side, point) "
        "DO UPDATE SET close_dec = excluded.close_dec, close_at = excluded.close_at", rows)
    return len(rows)


def logged_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM prop_lines").fetchone()[0])


def _no_vig_over(over: float | None, under: float | None) -> float | None:
    if not over or not under or over <= 1 or under <= 1:
        return None
    try:
        return devig([over, under], "power")[0]
    except ValueError:
        return None


@dataclass
class Graded:
    game_id: str
    player: str
    stat: str
    point: float
    p_open: float | None  # consensus no-vig P(over) at the first sighting
    p_close: float  # ... at the last pre-kickoff sighting
    p_model: float  # the projector's P(over)
    over: bool


def _actuals(weekly: pl.DataFrame) -> dict[tuple[int, int, str], dict]:
    cols = [c for c in set(STAT_COLUMNS.values()) if c in weekly.columns]
    out = {}
    for r in weekly.select("season", "week", "player_display_name", *cols).iter_rows(named=True):
        if r["player_display_name"]:
            out[(int(r["season"]), int(r["week"]), normalize_name(r["player_display_name"]))] = r
    return out


def graded_lines(conn: sqlite3.Connection, weekly: pl.DataFrame, projector, now: datetime) -> list[Graded]:
    """Lines with a two-sided sportsbook price, a played result and a projection (pushes dropped).

    The projection is rebuilt from stats before the game's week, without the live game context
    (spread, team total), so it is a little weaker than what the scan used. A player with no stat
    line for the week (inactive, or the stats have not updated) is not graded."""
    cutoff = (now - PLAYED_AFTER).astimezone(UTC).isoformat()
    games = {r["game_id"]: r for r in conn.execute(
        "SELECT game_id, season, week FROM games WHERE league = 'nfl' AND week IS NOT NULL AND kickoff_utc < ?",
        (cutoff,))}
    pairs: dict[tuple, dict[str, sqlite3.Row]] = defaultdict(dict)
    for r in conn.execute("SELECT * FROM prop_lines WHERE close_dec IS NOT NULL"):
        if r["game_id"] in games:
            pairs[(r["game_id"], r["player"], r["stat"], r["point"], r["book"])][r["side"]] = r
    prices: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: {"open": [], "close": []})
    for (gid, player, stat, point, _), sides in pairs.items():
        o, u = sides.get("over"), sides.get("under")
        if o is None or u is None:
            continue
        pc = _no_vig_over(o["close_dec"], u["close_dec"])
        if pc is None:
            continue
        prices[(gid, player, stat, point)]["close"].append(pc)
        po = _no_vig_over(o["open_dec"], u["open_dec"])
        if po is not None:
            prices[(gid, player, stat, point)]["open"].append(po)
    actual = _actuals(weekly)
    out = []
    for (gid, player, stat, point), p in prices.items():
        g = games[gid]
        row = actual.get((g["season"], g["week"], normalize_name(player)))
        col = STAT_COLUMNS.get(stat)
        if row is None or col is None or row.get(col) is None or float(row[col]) == point:
            continue
        proj = projector.project(player, stat, g["season"], g["week"])
        if proj is None:
            continue
        mean, sd = proj
        kind = "count" if stat == "receptions" else "yards"
        p_model = prop_p_over(point, mean, kind, sd if sd > 0 else None, stat=stat)
        out.append(Graded(gid, player, stat, point, float(np.median(p["open"])) if p["open"] else None,
                          float(np.median(p["close"])), p_model, float(row[col]) > point))
    return out


def _logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def grade(lines: list[Graded]) -> dict:
    """Does the projector add anything to the closing price?

    b_model/se: the projector's weight in a logistic blend with the closing no-vig price (the same
    test the game models face; z >= 1.96 is the bar). Picks: lines where the projector beats the
    close by EDGE_MIN on a side, and how often that side hit against what the close implied.
    Moves: of those picks with an opening price, how often the line moved toward the projector.
    """
    n = len(lines)
    if n == 0:
        return {"n": 0}
    pm = np.array([g.p_model for g in lines])
    pc = np.array([g.p_close for g in lines])
    y = np.array([float(g.over) for g in lines])
    fit = fit_blend(pm, pc, y)
    over_pick, under_pick = pm - pc >= EDGE_MIN, pc - pm >= EDGE_MIN
    picks = over_pick | under_pick
    won = np.where(over_pick, y == 1, y == 0)[picks]
    fair = np.where(over_pick, pc, 1 - pc)[picks]  # what the close said the picked side was worth
    moved = [(g.p_close - g.p_open) * (1 if g.p_model > g.p_close else -1)
             for g, pk in zip(lines, picks) if pk and g.p_open is not None and g.p_close != g.p_open]
    return {"n": n, "b_model": fit.b_model, "se": fit.b_model_se,
            "z": fit.b_model / fit.b_model_se if fit.b_model_se > 0 else 0.0,
            "brier_close": float(np.mean((pc - y) ** 2)), "brier_model": float(np.mean((pm - y) ** 2)),
            "logloss_close": _logloss(pc, y), "logloss_model": _logloss(pm, y),
            "picks": int(picks.sum()), "pick_hit": float(won.mean()) if picks.any() else 0.0,
            "pick_fair": float(fair.mean()) if picks.any() else 0.0,
            "moves": len(moved), "moved_toward": float(np.mean([m > 0 for m in moved])) if moved else 0.0,
            "enough": n >= MIN_GRADED}


def summary_lines(r: dict, logged: int) -> list[str]:
    """The props check in plain English."""
    if r["n"] == 0:
        return [f"Player line check: {logged:,} lines logged, none graded yet. A line is graded once its game is "
                "final and the NFL stats have updated (usually the next morning)."]
    out = [f"Player line check: {r['n']:,} graded lines (of {logged:,} logged). Accuracy score: closing price "
           f"{r['brier_close']:.4f}, our projection {r['brier_model']:.4f} (lower is better).",
           f"Does our projection add anything to the closing price? Weight {r['b_model']:+.2f} ± {r['se']:.2f} "
           f"(score {r['z']:.2f}; 1.96 or more would be a real edge)."]
    if r["picks"]:
        out.append(f"Where we disagreed by {EDGE_MIN:.0%} or more ({r['picks']:,} lines), our side hit "
                   f"{r['pick_hit']:.1%} against the {r['pick_fair']:.1%} the closing price implied "
                   f"({r['pick_hit'] - r['pick_fair']:+.1%}).")
    if r["moves"]:
        out.append(f"Lines moved toward our projection before kickoff {r['moved_toward']:.0%} of the time "
                   f"({r['moves']:,} lines; above 50% is a good sign).")
    if not r["enough"]:
        out.append(f"Too few lines to trust yet (want {MIN_GRADED:,} or more), so keep logging.")
    return out
