from dataclasses import replace
from datetime import datetime, timezone

import pytest

from gridiron_edge.config import Settings


@pytest.fixture
def settings(tmp_path):
    return replace(
        Settings(),
        odds_api_key="test-odds-key",
        cfbd_api_key="test-cfbd-key",
        therundown_api_key="test-rundown-key",
        notify_phone="+15555550100",
        db_path=tmp_path / "t.db",
    )


@pytest.fixture
def now():
    return datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    from gridiron_edge.db import connect

    c = connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def mk():
    from gridiron_edge.types import KalshiMarket

    def make(ticker, event, mtype, team=None, line=None, yes_bid=None, yes_ask=0.5,
             no_ask=None, yes_ask_size=1000, no_ask_size=1000, status="active", game_id="g"):
        if yes_bid is None:
            yes_bid = yes_ask - 0.01
        if no_ask is None:
            no_ask = 1 - yes_bid
        return KalshiMarket(
            ticker=ticker, event_ticker=event, series="S", market_type=mtype,
            game_id=game_id, team=team, line=line, player=None, stat=None,
            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=1 - yes_ask, no_ask=no_ask,
            yes_ask_size=yes_ask_size, no_ask_size=no_ask_size, status=status,
            result="", fee_multiplier=1.0,
            fetched_at=datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc),
        )

    return make


@pytest.fixture
def load():
    import json
    from pathlib import Path

    def _load(name):
        return json.loads((Path(__file__).parent / "fixtures" / name).read_text())

    return _load


class FakeNflverse:
    """Offline stand-in for NflverseSource: empty frames with the real column names."""

    def schedules(self, seasons):
        import polars as pl

        from gridiron_edge.clients.nflverse import SCHEDULE_COLS

        text, ints = ("game_id", "gameday", "home_team", "away_team"), ("season", "week")
        return pl.DataFrame(schema={c: pl.Utf8 if c in text else pl.Int64 if c in ints else pl.Float64
                                    for c in SCHEDULE_COLS})

    def weekly_player_stats(self, seasons):
        import polars as pl

        return pl.DataFrame(schema={"player_display_name": pl.Utf8, "season": pl.Int64,
                                    "week": pl.Int64, "recent_team": pl.Utf8,
                                    "receiving_yards": pl.Float64, "rushing_yards": pl.Float64,
                                    "passing_yards": pl.Float64})

    def team_epa(self, seasons):
        import polars as pl

        return pl.DataFrame(schema={"season": pl.Int64, "week": pl.Int64, "team": pl.Utf8,
                                    "off_epa": pl.Float64, "def_epa": pl.Float64, "plays": pl.Int64})


@pytest.fixture
def services_mocked(settings, load):
    """Builder for Services with every HTTP route mocked from the client fixtures.

    `build(**settings_overrides)` returns a Services on a temp db whose texts land in `build.sink`.
    `build.router` is the respx router; add routes before calling build to override defaults.
    """
    import httpx
    import respx
    from aiolimiter import AsyncLimiter

    from gridiron_edge.notify.console import ConsoleNotifier
    from gridiron_edge.services import build_services

    router = respx.MockRouter(assert_all_called=False, assert_all_mocked=True)
    sink: list[str] = []
    built = []
    state = {"defaults": False}

    def defaults():
        def get(response, **lookups):
            # respx would replace a test's route that has the same pattern, so only fill gaps
            if respx.Route(method="GET", **lookups) not in list(router.routes):
                router.get(**lookups).mock(return_value=response)

        rd_headers = {"X-Tier": "free", "X-Data-Delay-Seconds": "300", "X-Bookmakers": "19,23,22",
                      "X-Datapoints-Monthly-Remaining": "199000"}
        odds_headers = {"x-requests-remaining": "480", "x-requests-used": "20", "x-requests-last": "0"}
        odds = r"the-odds-api\.com/v4/sports/americanfootball_nfl"
        get(httpx.Response(200, json=load("odds_api_events.json"), headers=odds_headers),
            url__regex=odds + r"/events(\?.*)?$")
        get(httpx.Response(200, json=load("odds_api_event_props.json"), headers=odds_headers),
            url__regex=odds + r"/events/[^/]+/odds")
        get(httpx.Response(200, json=load("odds_api_odds.json"), headers=odds_headers), url__regex=odds + "/odds")
        get(httpx.Response(200, json=[], headers=odds_headers), url__regex=r"the-odds-api\.com/")
        get(httpx.Response(200, json={"events": []}, headers=rd_headers), url__regex=r"therundown\.io/")
        get(httpx.Response(200, json=load("cfbd_info.json")), url__regex=r"collegefootballdata\.com/info")
        get(httpx.Response(200, json=load("cfbd_games.json")), url__regex=r"collegefootballdata\.com/games\?")
        get(httpx.Response(200, json=[]), url__regex=r"collegefootballdata\.com/")
        get(httpx.Response(200, json=load("kalshi_series.json")), url__regex=r"kalshi\.com/.*/series/")
        get(httpx.Response(200, json={"markets": [], "cursor": ""}), url__regex=r"kalshi\.com/")

    def build(**overrides):
        if not state["defaults"]:  # appended after any test routes, so those match first
            defaults()
            state["defaults"] = True
        transport = httpx.MockTransport(router.async_handler)
        svc = build_services(replace(settings, **overrides),
                             transports=dict.fromkeys(("odds", "kalshi", "rundown", "cfbd"), transport))
        for client in (svc.odds, svc.kalshi, svc.rundown, svc.cfbd):
            if client is not None:  # no real rate limits against the mock
                client._http._limiter = AsyncLimiter(1000, 1)
        svc.nflverse = FakeNflverse()
        svc.notifier.channels = [ConsoleNotifier(sink)]
        built.append(svc)
        return svc

    build.router = router
    build.sink = sink
    yield build
    for svc in built:
        svc.conn.close()
