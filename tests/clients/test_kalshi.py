import httpx
import pytest

from gridiron_edge.clients.kalshi import KalshiClient


@pytest.fixture
def kalshi():
    return KalshiClient("https://k.test/trade-api/v2", resolve_ticker=lambda raw: ("g", raw.get("yes_sub_title")))


async def test_markets_paginates_and_parses(respx_mock, kalshi, load):
    route = respx_mock.get(url__regex=r".*/markets\?.*").mock(side_effect=[httpx.Response(200, json=load("kalshi_markets_p1.json")), httpx.Response(200, json=load("kalshi_markets_p2.json"))])
    ms = await kalshi.markets("KXNFLGAME")
    sea = next(m for m in ms if m.ticker == "KXNFLGAME-26NOV02CHISEA-SEA")
    assert (sea.yes_bid, sea.yes_ask, sea.no_ask, sea.market_type) == (0.58, 0.62, 0.42, "ml")
    assert route.call_count == 2 and len(ms) == 3
    assert (sea.yes_ask_size, sea.no_ask_size, sea.team, sea.game_id, sea.line) == (200.0, 3200.0, "Seattle", "g", None)
    assert route.calls[1].request.url.params["cursor"] == "abc123"
    assert route.calls[0].request.url.params["limit"] == "100"


async def test_spread_line_from_floor_strike(respx_mock, kalshi, load):
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json=load("kalshi_markets_p2.json")))
    assert (await kalshi.markets("KXNFLSPREAD"))[0].line == 7.5


async def test_orderbook_bids_sorted(respx_mock, kalshi, load):
    respx_mock.get(url__regex=r".*/orderbook").mock(return_value=httpx.Response(200, json=load("kalshi_orderbook.json")))
    ob = await kalshi.orderbook("T")
    assert ob["yes"][0][0] >= ob["yes"][-1][0] and ob["yes"][0] == (0.58, 3200.0)
    assert [p for p, _ in ob["no"]] == [0.38, 0.30, 0.01]


async def test_unknown_team_gets_no_game_id(respx_mock, load):
    k = KalshiClient("https://k.test/trade-api/v2", resolve_ticker=lambda raw: (None, None))
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json=load("kalshi_markets_p2.json")))
    assert all(m.game_id is None for m in await k.markets("KXNFLSPREAD"))


async def test_fee_multiplier_cached_and_defaults(respx_mock, kalshi, load):
    route = respx_mock.get(url__regex=r".*/series/KXNFLSPREAD").mock(
        return_value=httpx.Response(200, json={"series": {"fee_multiplier": 0.5}}))
    assert await kalshi.fee_multiplier("KXNFLSPREAD") == 0.5
    assert await kalshi.fee_multiplier("KXNFLSPREAD") == 0.5 and route.call_count == 1
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json=load("kalshi_markets_p2.json")))
    assert all(m.fee_multiplier == 0.5 for m in await kalshi.markets("KXNFLSPREAD"))
    respx_mock.get(url__regex=r".*/series/KXNFLTOTAL").mock(return_value=httpx.Response(404))
    assert await kalshi.fee_multiplier("KXNFLTOTAL") == 1.0


async def test_prop_market_parsed_and_unknown_series_skipped(respx_mock, kalshi):
    raw = {"ticker": "KXNFLRECYDS-26OCT22NECHI-NETHENDERSON32-25", "event_ticker": "KXNFLRECYDS-26OCT22NECHI",
           "yes_sub_title": "TreVeyon Henderson: 25+", "status": "active", "result": "yes", "floor_strike": 24.5,
           "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.45", "no_bid_dollars": "0.55", "no_ask_dollars": "0.60",
           "yes_ask_size_fp": "10.00", "no_ask_size_fp": "20.00"}
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json={"markets": [raw], "cursor": None}))
    (m,) = await kalshi.markets("KXNFLRECYDS")
    assert (m.market_type, m.player, m.stat, m.line, m.result) == ("prop", "TreVeyon Henderson", "reception_yds", 24.5, "yes")
    assert await kalshi.markets("KXNFLFOO") == []


async def test_rules_primary_kept_for_tie_handling(respx_mock, kalshi):
    raw = {"ticker": "KXNFLGAME-X-SEA", "event_ticker": "KXNFLGAME-X", "status": "active",
           "rules_primary": "If the game ends in a tie, the market resolves to a 50/50 split."}
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json={"markets": [raw], "cursor": None}))
    (m,) = await kalshi.markets("KXNFLGAME")
    assert "tie" in m.rules_primary
