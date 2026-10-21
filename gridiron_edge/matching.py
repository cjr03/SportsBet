"""Team-name canonicalization, the game registry, and Kalshi event-ticker parsing."""
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from gridiron_edge.db import register_schema
from gridiron_edge.types import Game

register_schema(
    """CREATE TABLE IF NOT EXISTS team_aliases (
        league TEXT NOT NULL, name_norm TEXT NOT NULL, canonical TEXT NOT NULL,
        PRIMARY KEY (league, name_norm))""")
register_schema(
    """CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY, league TEXT NOT NULL, season INTEGER NOT NULL,
        week INTEGER, kickoff_utc TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
        neutral INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'scheduled',
        home_pts INTEGER, away_pts INTEGER, period TEXT, clock TEXT,
        odds_event_id TEXT, updated_at TEXT NOT NULL)""")

_TEAMS_JSON = Path(__file__).parent / "data" / "nfl_teams.json"
_WINDOW = timedelta(hours=36)


_NAME_SUFFIX = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$")
# full-name aliases seen across the Odds API, Kalshi and nflverse (both sides map to one form)
_NAME_ALIASES = {
    "gabe davis": "gabriel davis", "hollywood brown": "marquise brown", "mitch trubisky": "mitchell trubisky",
    "josh palmer": "joshua palmer", "chig okonkwo": "chigoziem okonkwo", "tank dell": "nathaniel dell",
    "patrick freiermuth": "pat freiermuth", "cam ward": "cameron ward", "joshua dobbs": "josh dobbs",
    "scott miller": "scotty miller", "jeff wilson": "jeffery wilson", "ken walker": "kenneth walker",
    "bam knight": "zonovan knight",
}


@lru_cache(maxsize=16384)
def normalize_name(name: str) -> str:
    """Player-name join key: ASCII, lower-case, no punctuation or Jr/Sr/II-V suffix, known nicknames
    mapped (in the spirit of nflreadr's clean_player_names)."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`,]", "", s)  # D.J. -> dj, Ja'Marr -> jamarr
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = _NAME_SUFFIX.sub("", s)
    return _NAME_ALIASES.get(s, s)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class TeamResolver:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        teams = json.loads(_TEAMS_JSON.read_text())
        for abbr, info in teams.items():
            for n in [abbr, *info["names"], *info["kalshi"]]:
                self.add("nfl", n, abbr, replace=False)

    def add(self, league: str, name: str, canonical: str, replace: bool = True) -> None:
        norm = _norm(name)
        if not norm:
            return
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self.conn.execute(
            f"{verb} INTO team_aliases (league, name_norm, canonical) VALUES (?,?,?)",
            (league, norm, canonical))

    def canonical(self, league: str, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT canonical FROM team_aliases WHERE league=? AND name_norm=?",
            (league, _norm(name))).fetchone()
        return row[0] if row else None


def _row_to_game(r: sqlite3.Row) -> Game:
    return Game(
        game_id=r["game_id"], league=r["league"], season=r["season"], week=r["week"],
        kickoff=datetime.fromisoformat(r["kickoff_utc"]), home=r["home"], away=r["away"],
        neutral=bool(r["neutral"]), status=r["status"], home_pts=r["home_pts"],
        away_pts=r["away_pts"], period=r["period"], clock=r["clock"])


class GameRegistry:
    def __init__(self, conn: sqlite3.Connection, teams: TeamResolver):
        self.conn = conn
        self.teams = teams

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert(self, game: Game) -> None:
        # A schedule refresh must not regress status/scores or drop the odds event id.
        self.conn.execute(
            """INSERT INTO games (game_id, league, season, week, kickoff_utc, home, away,
                   neutral, status, home_pts, away_pts, period, clock, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id) DO UPDATE SET
                   league=excluded.league, season=excluded.season, week=excluded.week,
                   kickoff_utc=excluded.kickoff_utc, home=excluded.home, away=excluded.away,
                   neutral=excluded.neutral,
                   status=CASE WHEN excluded.status='scheduled' THEN games.status
                               ELSE excluded.status END,
                   home_pts=COALESCE(excluded.home_pts, games.home_pts),
                   away_pts=COALESCE(excluded.away_pts, games.away_pts),
                   period=COALESCE(excluded.period, games.period),
                   clock=COALESCE(excluded.clock, games.clock),
                   updated_at=excluded.updated_at""",
            (game.game_id, game.league, game.season, game.week, _utc(game.kickoff),
             game.home, game.away, int(game.neutral), game.status, game.home_pts,
             game.away_pts, game.period, game.clock, self._now()))

    def get(self, game_id: str) -> Game | None:
        r = self.conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return _row_to_game(r) if r else None

    def resolve(self, league: str, home_name: str, away_name: str,
                kickoff: datetime) -> str | None:
        home = self.teams.canonical(league, home_name)
        away = self.teams.canonical(league, away_name)
        if home is None or away is None:
            return None
        rows = self.conn.execute(
            "SELECT * FROM games WHERE league=? AND kickoff_utc BETWEEN ? AND ?",
            (league, _utc(kickoff - _WINDOW), _utc(kickoff + _WINDOW))).fetchall()
        best: tuple[timedelta, str] | None = None
        for r in rows:
            exact = r["home"] == home and r["away"] == away
            swapped = r["neutral"] and r["home"] == away and r["away"] == home
            if not (exact or swapped):
                continue
            gap = abs(datetime.fromisoformat(r["kickoff_utc"]) - kickoff)
            if best is None or gap < best[0]:
                best = (gap, r["game_id"])
        return best[1] if best else None

    def upcoming(self, within: timedelta, now: datetime) -> list[Game]:
        rows = self.conn.execute(
            """SELECT * FROM games WHERE status='scheduled' AND kickoff_utc BETWEEN ? AND ?
               ORDER BY kickoff_utc""", (_utc(now), _utc(now + within)))
        return [_row_to_game(r) for r in rows]

    def live(self) -> list[Game]:
        rows = self.conn.execute("SELECT * FROM games WHERE status='live' ORDER BY kickoff_utc")
        return [_row_to_game(r) for r in rows]

    def all_since(self, dt: datetime) -> list[Game]:
        rows = self.conn.execute(
            "SELECT * FROM games WHERE kickoff_utc >= ? ORDER BY kickoff_utc", (_utc(dt),))
        return [_row_to_game(r) for r in rows]

    def update_score(self, game_id: str, status: str, home_pts: int | None,
                     away_pts: int | None, period: str | None, clock: str | None) -> None:
        self.conn.execute(
            """UPDATE games SET status=?, home_pts=?, away_pts=?, period=?, clock=?,
               updated_at=? WHERE game_id=?""",
            (status, home_pts, away_pts, period, clock, self._now(), game_id))

    def set_odds_event(self, game_id: str, event_id: str) -> None:
        self.conn.execute(
            "UPDATE games SET odds_event_id=?, updated_at=? WHERE game_id=?",
            (event_id, self._now(), game_id))


_EVENT_RE = re.compile(r"^([A-Z0-9]+)-(\d{2})([A-Z]{3})(\d{2})([A-Z]{4,8})$")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# 3+3 is by far the most common; try it first, then the lopsided splits.
_SPLITS = (3, 2, 4)


def parse_kalshi_event(event_ticker: str,
                       known_codes: Iterable[str] | None = None) -> tuple[date, str, str] | None:
    """'KXNFLGAME-26NOV02CHISEA' -> (date, away, home); None if it doesn't parse."""
    m = _EVENT_RE.match(event_ticker)
    if not m or m.group(3) not in _MONTHS:
        return None
    try:
        d = date(2000 + int(m.group(2)), _MONTHS.index(m.group(3)) + 1, int(m.group(4)))
    except ValueError:
        return None
    block = m.group(5)
    splits = [(block[:i], block[i:]) for i in _SPLITS
              if 2 <= len(block) - i <= 4 and 2 <= i <= 4]
    if not splits:
        return None
    if known_codes is not None:
        known = set(known_codes)
        for away, home in splits:
            if away in known and home in known:
                return d, away, home
    away, home = splits[0]
    return d, away, home


def kalshi_resolver(registry: GameRegistry,
                    teams: TeamResolver) -> Callable[[dict], tuple[str | None, str | None]]:
    """Returns f(market dict with event_ticker, yes_sub_title) -> (game_id, canonical team)."""
    def resolve(market: dict) -> tuple[str | None, str | None]:
        ticker = market.get("event_ticker") or ""
        league = "cfb" if ticker.startswith("KXNCAAF") else "nfl"
        known = {r[0] for r in teams.conn.execute(
            "SELECT name_norm FROM team_aliases WHERE league=?", (league,))}
        parsed = parse_kalshi_event(ticker, {n.upper() for n in known})
        if parsed is None:
            return None, None
        d, away, home = parsed
        kickoff = datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)
        game_id = registry.resolve(league, home, away, kickoff)
        if game_id is None:
            return None, None
        team = teams.canonical(league, market.get("yes_sub_title") or "")
        game = registry.get(game_id)
        if team is not None and game is not None and team not in (game.home, game.away):
            team = None
        return game_id, team
    return resolve
