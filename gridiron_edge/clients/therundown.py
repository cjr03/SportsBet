"""TheRundown client: delayed live scores and main-line prices for DK/FD/BetMGM/Kalshi."""
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime

from gridiron_edge.budget import BudgetGuard
from gridiron_edge.clients.http import AuthError, RateLimitedClient
from gridiron_edge.strategy.odds import american_to_decimal
from gridiron_edge.types import Game, League, Quote

log = logging.getLogger(__name__)
BASE_URL = "https://therundown.io/api/v2"
SPORT_ID = {"cfb": 1, "nfl": 2}
MONTHLY_CAP = 200_000
RESERVE_FRAC = 0.10
DAILY_CAP = 20_000
DAILY_RESERVE_FRAC = 0.10  # non-essential calls stop when the day's points fall below this share
SENTINEL = 0.0001  # price meaning "off the board"
AFFILIATE = {19: "draftkings", 23: "fanduel", 22: "betmgm", 25: "kalshi"}
MARKET = {1: "h2h", 2: "spreads", 3: "totals"}
_LIVE = {"STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD",
         "STATUS_END_OF_REGULATION", "STATUS_OVERTIME", "STATUS_DELAYED"}
_VOID = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED", "STATUS_FORFEIT"}


def map_status(raw: str | None) -> str:
    if raw == "STATUS_SCHEDULED":
        return "scheduled"
    if raw == "STATUS_FINAL":
        return "final"
    if raw in _LIVE:
        return "live"
    if raw in _VOID:
        return "void_candidate"
    return "unknown"


def _int(raw) -> int | None:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _team_name(t: dict) -> str:
    return f"{t.get('name') or ''} {t.get('mascot') or ''}".strip()


def _kickoff(ev: dict) -> datetime:
    return datetime.fromisoformat(ev["event_date"].replace("Z", "+00:00"))


class TheRundownClient:
    def __init__(self, key: str, conn, resolve: Callable[[League, str, str, datetime], str | None],
                 client: RateLimitedClient | None = None):
        self.resolve = resolve
        self._http = client or RateLimitedClient(
            BASE_URL, headers={"X-TheRundown-Key": key}, rate=1 / 1.1)
        self.budget = BudgetGuard(conn, "therundown", MONTHLY_CAP, RESERVE_FRAC)
        self.disabled = False
        self.unmatched = 0
        self.malformed = 0
        self.last_status = ""
        self.delay_seconds: int | None = None
        self.tier: str | None = None
        self.bookmakers_header: str | None = None
        # (UTC date of the reading, X-Datapoints-Remaining): the daily cap resets each day
        self.daily: tuple[date, int] | None = None
        self._cost: dict[tuple, int] = {}  # last X-Datapoints per (league, params) as a cost estimate

    def daily_remaining(self, today: date | None = None) -> int | None:
        today = today or datetime.now(UTC).date()
        if self.daily is None or self.daily[0] != today:
            return None
        return self.daily[1]

    def _allowed(self, cost: int, essential: bool) -> bool:
        daily = self.daily_remaining()
        if daily is not None:
            floor = 0 if essential else DAILY_RESERVE_FRAC * DAILY_CAP
            if daily <= 0 or daily - cost < floor:
                return False
        return self.budget.can_spend(cost, essential)

    async def _events(self, league: League, day: date, params: dict, essential: bool = False) -> list[dict]:
        if self.disabled:
            self.last_status = "disabled"
            return []
        kind = (league, tuple(sorted(params.items())))
        if not self._allowed(self._cost.get(kind, 0), essential):
            self.last_status = "skipped_budget"
            return []
        try:
            resp = await self._http.get(f"/sports/{SPORT_ID[league]}/events/{day.isoformat()}", params)
        except AuthError:
            self.disabled = True
            self.last_status = "auth_error"
            raise
        h = resp.headers
        remaining = _int(h.get("X-Datapoints-Monthly-Remaining"))
        cost = _int(h.get("X-Datapoints"))
        if cost is not None:
            self._cost[kind] = cost
        if remaining is not None:
            self.budget.record(remaining, MONTHLY_CAP - remaining, cost or 0)
        if (daily := _int(h.get("X-Datapoints-Remaining"))) is not None:
            self.daily = (datetime.now(UTC).date(), daily)
        if (delay := _int(h.get("X-Data-Delay-Seconds"))) is not None:
            self.delay_seconds = delay
        self.tier = h.get("X-Tier", self.tier)
        self.bookmakers_header = h.get("X-Bookmakers", self.bookmakers_header)
        self.last_status = "ok"
        body = resp.json()
        return body.get("events", []) if isinstance(body, dict) else body

    def _match(self, league: League, ev: dict) -> tuple[str, dict, dict] | None:
        home = next((t for t in ev.get("teams", []) if t.get("is_home")), None)
        away = next((t for t in ev.get("teams", []) if t.get("is_away")), None)
        game_id = None
        if home and away:
            try:
                game_id = self.resolve(league, _team_name(home), _team_name(away), _kickoff(ev))
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                log.warning("therundown: skipping malformed event %s: %r", ev.get("event_id"), e)
                self.malformed += 1
                return None
        if game_id is None:
            self.unmatched += 1
            return None
        return game_id, home, away

    async def scores(self, league: League, day: date, essential: bool = False) -> list[Game]:
        """essential: an open bet needs these scores to settle, so the daily/monthly reserve may be used."""
        out: list[Game] = []
        for ev in await self._events(league, day, {"affiliate_ids": "0"}, essential):
            m = self._match(league, ev)
            if m is None:
                continue
            game_id, _, _ = m
            sc = ev.get("score") or {}
            kickoff = _kickoff(ev)  # _match already parsed it successfully
            codes = game_id.split(":")[-1].split("@")
            if len(codes) != 2 or not all(codes):
                log.warning("therundown: unrecognised game_id %r (want ...:AWAY@HOME), skipping", game_id)
                self.malformed += 1
                continue
            away_code, home_code = codes
            period = sc.get("game_period")
            out.append(Game(
                game_id, league, kickoff.year if kickoff.month >= 7 else kickoff.year - 1,
                None, kickoff, home_code, away_code,
                status=map_status(sc.get("event_status")),
                home_pts=_int(sc.get("score_home")), away_pts=_int(sc.get("score_away")),
                period=None if period is None else str(period), clock=sc.get("display_clock")))
        return out

    async def lines(self, league: League, day: date) -> list[Quote]:
        params = {"market_ids": "1,2,3", "affiliate_ids": "19,23,22,25", "main_line": "true"}
        out: list[Quote] = []
        for ev in await self._events(league, day, params):
            m = self._match(league, ev)
            if m is None:
                continue
            game_id, home, away = m
            try:
                out.extend(self._event_quotes(ev, game_id, home, away))
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                log.warning("therundown: skipping malformed event %s: %r", ev.get("event_id"), e)
                self.malformed += 1
        return out

    def _event_quotes(self, ev: dict, game_id: str, home: dict, away: dict) -> list[Quote]:
        out: list[Quote] = []
        fetched = datetime.now(UTC)
        team_side = {home["team_id"]: "home", away["team_id"]: "away"}
        for mk in ev.get("markets", []):
            market = MARKET.get(mk.get("market_id"))
            if market is None or mk.get("period_id", 0) != 0:
                continue
            for part in mk.get("participants", []):
                if part.get("type") == "TYPE_TEAM":
                    side = team_side.get(part.get("id"))
                else:
                    side = {"Over": "over", "Under": "under"}.get(part.get("name"))
                if side is None or (market == "totals") != (side in ("over", "under")):
                    continue
                for line in part.get("lines", []):
                    for aff, price in line.get("prices", {}).items():
                        book = AFFILIATE.get(_int(aff))
                        p = price.get("price")
                        if book is None or price.get("closed_at") or p is None or p == SENTINEL or p == 0:
                            continue
                        try:
                            value = None if market == "h2h" or line.get("value") is None else float(line["value"])
                            ts = price.get("updated_at")
                            at = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else fetched
                            out.append(Quote("therundown", book, game_id, market, side, value,
                                             american_to_decimal(float(p)), at))
                        except (ValueError, TypeError, AttributeError) as e:
                            log.warning("therundown: skipping malformed price %s/%s: %r", game_id, book, e)
                            self.malformed += 1
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
