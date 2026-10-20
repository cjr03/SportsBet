import httpx
import pytest
from tenacity import wait_none

from gridiron_edge.budget import BudgetGuard
from gridiron_edge.clients.http import ApiError, AuthError, RateLimitedClient


async def test_retry_then_success(respx_mock):
    r = respx_mock.get("https://x.test/a").mock(side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": 1})])
    c = RateLimitedClient("https://x.test", rate=100, wait=wait_none())
    assert (await c.get("/a")).json() == {"ok": 1} and r.call_count == 2
    await c.aclose()


async def test_auth_error(respx_mock):
    respx_mock.get("https://x.test/a").mock(return_value=httpx.Response(401))
    c = RateLimitedClient("https://x.test", rate=100, wait=wait_none())
    with pytest.raises(AuthError):
        await c.get("/a")
    await c.aclose()


async def test_other_4xx_is_api_error_without_retry(respx_mock):
    r = respx_mock.get("https://x.test/a").mock(return_value=httpx.Response(404))
    c = RateLimitedClient("https://x.test", rate=100, wait=wait_none())
    with pytest.raises(ApiError) as e:
        await c.get("/a")
    assert not isinstance(e.value, AuthError) and r.call_count == 1
    await c.aclose()


async def test_gives_up_after_four_attempts(respx_mock):
    r = respx_mock.get("https://x.test/a").mock(return_value=httpx.Response(503))
    c = RateLimitedClient("https://x.test", rate=100, wait=wait_none())
    with pytest.raises(ApiError):
        await c.get("/a")
    assert r.call_count == 4
    await c.aclose()


async def test_retry_after_honoured(respx_mock, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("gridiron_edge.clients.http.asyncio.sleep", fake_sleep)
    respx_mock.get("https://x.test/a").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200)]
    )
    c = RateLimitedClient("https://x.test", rate=100, wait=wait_none())
    assert (await c.get("/a")).status_code == 200
    assert sleeps == [3.0]
    await c.aclose()


def test_budget_reserve(conn):
    b = BudgetGuard(conn, "odds_api", 500, 0.10)
    assert b.remaining() is None and b.can_spend(1000) and not b.exhausted
    b.record(60, 440, 3)
    assert b.remaining() == 60
    assert b.can_spend(9) and not b.can_spend(11) and b.can_spend(11, essential=True)
    assert not b.can_spend(61, essential=True)
    b.record(0, 500, 3)
    assert b.exhausted and not b.can_spend(1, essential=True)
