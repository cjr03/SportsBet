import httpx
import pytest

from gridiron_edge.clients.http import AuthError
from gridiron_edge.clients.odds_api import OddsApiClient


@pytest.fixture
def odds_client(conn):
    return OddsApiClient("k", conn, resolve=lambda lg, h, a, t: "nfl:20261102:CHI@SEA")


async def test_game_odds_parses_and_records_quota(respx_mock, odds_client, load):
    route = respx_mock.get(url__regex=r".*/sports/americanfootball_nfl/odds.*").mock(
        return_value=httpx.Response(200, json=load("odds_api_odds.json"),
                                    headers={"x-requests-remaining": "497", "x-requests-used": "3", "x-requests-last": "3"}))
    qs = await odds_client.game_odds("nfl")
    assert {q.market for q in qs} == {"h2h", "spreads", "totals"} and all(q.decimal > 1 for q in qs)
    assert odds_client.budget.remaining() == 497 and "pinnacle" in odds_client.seen_books
    sea = [q for q in qs if q.book == "pinnacle" and q.market == "h2h" and q.side == "home"]
    assert sea[0].decimal == 1.62 and sea[0].game_id == "nfl:20261102:CHI@SEA"
    assert {q.side for q in qs if q.market == "totals"} == {"over", "under"}
    params = route.calls[0].request.url.params
    assert params["markets"] == "h2h,spreads,totals" and params["oddsFormat"] == "decimal"
    assert params["apiKey"] == "k"


async def test_budget_refusal_spends_nothing(respx_mock, odds_client):
    odds_client.budget.record(10, 490, 3)
    route = respx_mock.get(url__regex=r".*/odds.*")
    assert await odds_client.game_odds("nfl") == [] and route.call_count == 0
    assert odds_client.last_status == "skipped_budget"


async def test_props_skip_alternate_and_mark_prizepicks(respx_mock, odds_client, load):
    respx_mock.get(url__regex=r".*/events/.*/odds.*").mock(
        return_value=httpx.Response(200, json=load("odds_api_event_props.json"), headers={"x-requests-remaining": "480"}))
    ps = await odds_client.event_props("nfl", "evt1")
    assert ps and all(not p.stat.endswith("_alternate") for p in ps)
    assert any(p.book == "prizepicks" and p.decimal is None and p.multiplier == 1.0 for p in ps)
    assert all(p.stat == "reception_yds" and p.player == "DK Metcalf" for p in ps)
    assert any(p.book == "draftkings" and p.decimal == 1.87 and p.multiplier is None for p in ps)


async def test_401_disables(respx_mock, odds_client):
    respx_mock.get(url__regex=r".*/odds.*").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await odds_client.game_odds("nfl")
    assert odds_client.disabled and await odds_client.game_odds("nfl") == []


async def test_unmatched_game_skipped(respx_mock, odds_client, load):
    odds_client.resolve = lambda *a: None
    respx_mock.get(url__regex=r".*/odds.*").mock(return_value=httpx.Response(200, json=load("odds_api_odds.json")))
    assert await odds_client.game_odds("nfl") == [] and odds_client.unmatched == 1


async def test_events_and_scores(respx_mock, odds_client, load):
    respx_mock.get(url__regex=r".*/events(\?.*)?$").mock(
        return_value=httpx.Response(200, json=load("odds_api_events.json")))
    respx_mock.get(url__regex=r".*/scores.*").mock(return_value=httpx.Response(200, json=[{"id": "evt1"}]))
    assert (await odds_client.events("nfl"))[0]["id"] == "evt1"
    assert await odds_client.scores("nfl") == [{"id": "evt1"}]


async def test_game_odds_stamp_fetch_time_and_sharp_books(respx_mock, odds_client, load):
    from gridiron_edge.clients.odds_api import REF_BOOKS
    route = respx_mock.get(url__regex=r".*/sports/americanfootball_nfl/odds.*").mock(
        return_value=httpx.Response(200, json=load("odds_api_odds.json"),
                                    headers={"x-requests-remaining": "497", "x-requests-used": "3"}))
    qs = await odds_client.game_odds("nfl")
    assert qs and all(q.fetched_at is not None and q.fetched_at.tzinfo is not None for q in qs)
    books = route.calls[0].request.url.params["bookmakers"].split(",")
    assert {"pinnacle", "lowvig", "betonlineag"} <= set(books) and len(books) <= 10  # one region of credits
    assert books == REF_BOOKS.split(",")
