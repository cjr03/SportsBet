import httpx
import pytest

from gridiron_edge.clients.cfbd import CfbdClient
from gridiron_edge.clients.http import AuthError


@pytest.fixture
def cfbd(conn):
    return CfbdClient("k", conn)


async def test_cfbd_bearer_and_cache(respx_mock, cfbd, load):
    route = respx_mock.get(url__regex=r".*/games.*").mock(
        return_value=httpx.Response(200, json=load("cfbd_games.json")))
    first = await cfbd.games(2024)
    assert route.calls[0].request.headers["Authorization"] == "Bearer k"
    assert route.calls[0].request.url.params["seasonType"] == "regular"
    assert await cfbd.games(2024) == first and route.call_count == 1
    row = cfbd._conn.execute("SELECT immutable FROM api_cache").fetchone()
    assert row["immutable"] == 1


async def test_cfbd_current_season_expires(respx_mock, cfbd, load, conn):
    route = respx_mock.get(url__regex=r".*/games.*").mock(
        return_value=httpx.Response(200, json=load("cfbd_games.json")))
    year = cfbd.current_season()
    await cfbd.games(year)
    await cfbd.games(year)
    assert route.call_count == 1
    conn.execute("UPDATE api_cache SET fetched_at='2000-01-01T00:00:00+00:00'")
    await cfbd.games(year)
    assert route.call_count == 2
    assert conn.execute("SELECT immutable FROM api_cache").fetchone()["immutable"] == 0


async def test_cfbd_records_info(respx_mock, cfbd, load):
    respx_mock.get(url__regex=r".*/info").mock(return_value=httpx.Response(200, json=load("cfbd_info.json")))
    info = await cfbd.info()
    assert info["remainingCalls"] == 998 and cfbd.budget.remaining() == 998


async def test_cfbd_401_disables(respx_mock, cfbd):
    respx_mock.get(url__regex=r".*/ratings/sp.*").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await cfbd.ratings_sp(2024)
    assert cfbd.disabled and await cfbd.ratings_sp(2024) == []
