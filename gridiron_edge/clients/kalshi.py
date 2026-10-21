"""Kalshi public market-data client (read-only, no auth)."""
import re
from collections.abc import Callable
from datetime import datetime, timezone

from gridiron_edge.clients.http import ApiError, RateLimitedClient
from gridiron_edge.types import KalshiMarket

GAME_SERIES = {
    "nfl": {"ml": "KXNFLGAME", "spread": "KXNFLSPREAD", "total": "KXNFLTOTAL"},
    "cfb": {"ml": "KXNCAAFGAME", "spread": "KXNCAAFSPREAD", "total": "KXNCAAFTOTAL"},
}
PROP_SERIES_STAT = {"KXNFLRECYDS": "reception_yds", "KXNFLRUSHYDS": "rush_yds", "KXNFLPASSYDS": "pass_yds"}
_SERIES_TYPE = {s: t for series in GAME_SERIES.values() for t, s in series.items()} | dict.fromkeys(PROP_SERIES_STAT, "prop")
PAGE_LIMIT = 100
_PROP_SUFFIX = re.compile(r":\s*\d+\+\s*$")


def series_type(ticker: str | None) -> str | None:
    """A market ticker's type from its series ("ml", "spread", "total", "prop"); None if unknown."""
    return _SERIES_TYPE.get((ticker or "").split("-")[0])


def _num(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


class KalshiClient:
    def __init__(self, base_url: str, resolve_ticker: Callable[[dict], tuple[str | None, str | None]],
                 client: RateLimitedClient | None = None):
        self.resolve_ticker = resolve_ticker
        self._http = client or RateLimitedClient(base_url, rate=2.0)
        self._fees: dict[str, float] = {}

    def _parse(self, raw: dict, series: str, fetched_at: datetime) -> KalshiMarket | None:
        mtype = _SERIES_TYPE.get(series)
        if mtype is None:
            return None
        game_id, team = self.resolve_ticker(raw)
        strike = raw.get("floor_strike")
        player = stat = None
        if mtype == "prop":
            player = _PROP_SUFFIX.sub("", raw.get("yes_sub_title") or "").strip() or None
            stat = PROP_SERIES_STAT[series]
        return KalshiMarket(
            ticker=raw["ticker"], event_ticker=raw["event_ticker"], series=series, market_type=mtype,
            game_id=game_id, team=team,
            line=None if mtype == "ml" or strike is None else float(strike),
            player=player, stat=stat,
            yes_bid=_num(raw.get("yes_bid_dollars")), yes_ask=_num(raw.get("yes_ask_dollars")),
            no_bid=_num(raw.get("no_bid_dollars")), no_ask=_num(raw.get("no_ask_dollars")),
            yes_ask_size=_num(raw.get("yes_ask_size_fp")),
            # buying NO lifts the YES bid, so its size is the NO ask size when not given directly
            no_ask_size=_num(raw.get("no_ask_size_fp", raw.get("yes_bid_size_fp"))),
            status=raw.get("status") or "", result=raw.get("result") or "",
            fee_multiplier=self._fees.get(series, 1.0), fetched_at=fetched_at,
            rules_secondary=raw.get("rules_secondary") or "",
            rules_primary=raw.get("rules_primary") or "",
        )

    async def markets(self, series: str, status: str = "open") -> list[KalshiMarket]:
        """All markets of one series. Uses the cached fee multiplier (1.0 until fee_multiplier() ran)."""
        if series not in _SERIES_TYPE:
            return []
        out: list[KalshiMarket] = []
        cursor = ""
        now = datetime.now(timezone.utc)
        while True:
            params = {"series_ticker": series, "status": status, "limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            body = (await self._http.get("/markets", params)).json()
            for raw in body.get("markets", []):
                m = self._parse(raw, series, now)
                if m is not None:
                    out.append(m)
            cursor = body.get("cursor") or ""
            if not cursor:
                return out

    async def market(self, ticker: str) -> KalshiMarket:
        body = (await self._http.get(f"/markets/{ticker}")).json()
        raw = body["market"]
        series = raw["event_ticker"].split("-")[0]
        m = self._parse(raw, series, datetime.now(timezone.utc))
        if m is None:
            raise ApiError(f"unsupported Kalshi series {series} for {ticker}")
        return m

    async def orderbook(self, ticker: str) -> dict[str, list[tuple[float, float]]]:
        body = (await self._http.get(f"/markets/{ticker}/orderbook")).json()
        book = body.get("orderbook_fp") or {}
        return {side: sorted(((float(p), float(s)) for p, s in book.get(f"{side}_dollars") or []),
                             key=lambda lvl: lvl[0], reverse=True)
                for side in ("yes", "no")}

    async def fee_multiplier(self, series: str) -> float:
        if series not in self._fees:
            try:
                body = (await self._http.get(f"/series/{series}")).json()
                self._fees[series] = float(body["series"].get("fee_multiplier", 1.0))
            except (ApiError, KeyError, TypeError, ValueError):
                return 1.0  # not cached, so a transient failure is retried next call
        return self._fees[series]

    async def aclose(self) -> None:
        await self._http.aclose()
