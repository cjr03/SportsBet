"""The Odds API client: game odds, player props, events and scores, with quota tracking."""
from collections.abc import Callable
from datetime import datetime

from gridiron_edge.budget import BudgetGuard
from gridiron_edge.clients.http import AuthError, RateLimitedClient
from gridiron_edge.types import League, PropQuote, Quote

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf"}
REF_BOOKS = "pinnacle,novig,prophetx,draftkings,fanduel,betmgm,betrivers,espnbet,hardrockbet,kalshi"
PROP_BOOKS = "prizepicks,draftkings,fanduel,betmgm,betrivers,espnbet"
PROP_MARKETS = ("player_pass_yds", "player_rush_yds", "player_reception_yds")
MONTHLY_CAP = 500
RESERVE_FRAC = 0.10
GAME_ODDS_COST = 3
PROPS_COST = 3
SCORES_COST = 2


def _int(raw: str | None) -> int | None:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


class OddsApiClient:
    def __init__(self, key: str, conn, resolve: Callable[[League, str, str, datetime], str | None],
                 client: RateLimitedClient | None = None):
        self._key = key
        self.resolve = resolve
        self._http = client or RateLimitedClient(BASE_URL, rate=5.0)
        self.budget = BudgetGuard(conn, "odds_api", MONTHLY_CAP, RESERVE_FRAC)
        self.disabled = False
        self.seen_books: set[str] = set()
        self.unmatched = 0
        self.last_status = ""

    async def _get(self, path: str, params: dict):
        params = {**params, "apiKey": self._key}
        try:
            resp = await self._http.get(path, params)
        except AuthError:
            self.disabled = True
            self.last_status = "auth_error"
            raise
        h = resp.headers
        remaining, used, last = (_int(h.get(f"x-requests-{k}")) for k in ("remaining", "used", "last"))
        if remaining is not None:
            self.budget.record(remaining, used or 0, last or 0)
        self.last_status = "ok"
        return resp.json()

    def _blocked(self, cost: int, essential: bool) -> bool:
        if self.disabled:
            self.last_status = "disabled"
            return True
        if not self.budget.can_spend(cost, essential):
            self.last_status = "skipped_budget"
            return True
        return False

    async def events(self, league: League) -> list[dict]:
        if self.disabled:
            return []
        return await self._get(f"/sports/{SPORT[league]}/events", {"dateFormat": "iso"})

    async def game_odds(self, league: League, commence_from: str | None = None,
                        commence_to: str | None = None, essential: bool = False) -> list[Quote]:
        if self._blocked(GAME_ODDS_COST, essential):
            return []
        params = {"markets": "h2h,spreads,totals", "oddsFormat": "decimal",
                  "dateFormat": "iso", "bookmakers": REF_BOOKS}
        if commence_from:
            params["commenceTimeFrom"] = commence_from
        if commence_to:
            params["commenceTimeTo"] = commence_to
        out: list[Quote] = []
        for ev in await self._get(f"/sports/{SPORT[league]}/odds", params):
            home, away = ev["home_team"], ev["away_team"]
            game_id = self.resolve(league, home, away, datetime.fromisoformat(ev["commence_time"]))
            if game_id is None:
                self.unmatched += 1
                continue
            for bk in ev.get("bookmakers", []):
                self.seen_books.add(bk["key"])
                for mk in bk.get("markets", []):
                    if mk["key"] not in ("h2h", "spreads", "totals"):
                        continue
                    stamp = datetime.fromisoformat(mk.get("last_update") or bk["last_update"])
                    for o in mk["outcomes"]:
                        side = {home: "home", away: "away", "Over": "over", "Under": "under"}.get(o["name"])
                        if side is None:
                            continue
                        out.append(Quote("odds_api", bk["key"], game_id, mk["key"], side,
                                         o.get("point"), float(o["price"]), stamp))
        return out

    async def event_props(self, league: League, event_id: str, essential: bool = False) -> list[PropQuote]:
        if self._blocked(PROPS_COST, essential):
            return []
        params = {"bookmakers": PROP_BOOKS, "markets": ",".join(PROP_MARKETS),
                  "oddsFormat": "decimal", "includeMultipliers": "true", "dateFormat": "iso"}
        ev = await self._get(f"/sports/{SPORT[league]}/events/{event_id}/odds", params)
        game_id = self.resolve(league, ev["home_team"], ev["away_team"],
                               datetime.fromisoformat(ev["commence_time"]))
        if game_id is None:
            self.unmatched += 1
            return []
        out: list[PropQuote] = []
        for bk in ev.get("bookmakers", []):
            self.seen_books.add(bk["key"])
            is_pp = bk["key"] == "prizepicks"
            for mk in bk.get("markets", []):
                if mk["key"].endswith("_alternate") or mk["key"] not in PROP_MARKETS:
                    continue
                stamp = datetime.fromisoformat(mk.get("last_update") or bk["last_update"])
                for o in mk["outcomes"]:
                    side = {"Over": "over", "Under": "under"}.get(o["name"])
                    if side is None or o.get("point") is None:
                        continue
                    mult = o.get("multiplier") if is_pp else None
                    out.append(PropQuote("odds_api", bk["key"], game_id, o["description"],
                                         mk["key"].removeprefix("player_"), side, float(o["point"]),
                                         None if is_pp else float(o["price"]), stamp,
                                         None if mult is None else float(mult)))
        return out

    async def scores(self, league: League, days_from: int = 1) -> list[dict]:
        if self._blocked(SCORES_COST, False):
            return []
        return await self._get(f"/sports/{SPORT[league]}/scores", {"daysFrom": days_from, "dateFormat": "iso"})

    async def aclose(self) -> None:
        await self._http.aclose()
