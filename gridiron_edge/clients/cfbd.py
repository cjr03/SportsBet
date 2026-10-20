"""CollegeFootballData v2 client: raw httpx, one request at a time, cached in api_cache."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from gridiron_edge.budget import BudgetGuard
from gridiron_edge.clients.http import AuthError, RateLimitedClient

BASE_URL = "https://api.collegefootballdata.com"
MONTHLY_CAP = 1000
RESERVE_FRAC = 0.10
CACHE_TTL = timedelta(hours=6)


class CfbdClient:
    def __init__(self, key: str, conn, client: RateLimitedClient | None = None):
        self._conn = conn
        self._http = client or RateLimitedClient(
            BASE_URL, headers={"Authorization": f"Bearer {key}"}, rate=2.0)
        self._lock = asyncio.Lock()  # concurrent requests risk a Cloudflare block
        self.budget = BudgetGuard(conn, "cfbd", MONTHLY_CAP, RESERVE_FRAC)
        self.disabled = False
        self.last_status = ""

    @staticmethod
    def current_season(today: datetime | None = None) -> int:
        today = today or datetime.now(timezone.utc)
        return today.year if today.month >= 7 else today.year - 1

    async def _fetch(self, path: str, params: dict):
        try:
            resp = await self._http.get(path, params)
        except AuthError:
            self.disabled = True
            self.last_status = "auth_error"
            raise
        self.last_status = "ok"
        return resp.json()

    async def info(self) -> dict:
        async with self._lock:
            data = await self._fetch("/info", {})
        if (remaining := data.get("remainingCalls")) is not None:
            self.budget.record(int(remaining), int(data.get("usedCalls") or 0), 0)
        return data

    async def _get(self, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if self.disabled:
            self.last_status = "disabled"
            return []
        key = f"cfbd:{path}?{urlencode(sorted(params.items()))}"
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, immutable, body FROM api_cache WHERE key=?", (key,)).fetchone()
            if row and (row["immutable"] or now - datetime.fromisoformat(row["fetched_at"]) < CACHE_TTL):
                return json.loads(row["body"])
            if self.budget.exhausted:
                self.last_status = "skipped_budget"
                return json.loads(row["body"]) if row else []
            data = await self._fetch(path, params)
            immutable = int(int(params.get("year", self.current_season(now))) < self.current_season(now))
            self._conn.execute(
                "INSERT OR REPLACE INTO api_cache (key, fetched_at, immutable, body) VALUES (?,?,?,?)",
                (key, now.isoformat(), immutable, json.dumps(data)))
            return data

    async def games(self, year: int, week: int | None = None, season_type: str = "regular"):
        return await self._get("/games", year=year, week=week, seasonType=season_type)

    async def lines(self, year: int, week: int):
        return await self._get("/lines", year=year, week=week)

    async def ppa_games(self, year: int, week: int | None = None):
        return await self._get("/ppa/games", year=year, week=week)

    async def ratings_sp(self, year: int):
        return await self._get("/ratings/sp", year=year)

    async def ratings_fpi(self, year: int):
        return await self._get("/ratings/fpi", year=year)

    async def talent(self, year: int):
        return await self._get("/talent", year=year)

    async def returning(self, year: int):
        return await self._get("/player/returning", year=year)

    async def teams_fbs(self, year: int):
        return await self._get("/teams/fbs", year=year)

    async def player_game_stats(self, year: int, week: int):
        return await self._get("/games/players", year=year, week=week)

    async def aclose(self) -> None:
        await self._http.aclose()
