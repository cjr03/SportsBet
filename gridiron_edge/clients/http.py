"""Shared async HTTP base: rate limiting, bounded retries, typed errors."""
import asyncio

import httpx
from aiolimiter import AsyncLimiter
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt
from tenacity import wait_exponential_jitter

MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 60.0


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class AuthError(ApiError):
    """401/403: bad or missing key. Callers stop polling that API."""


class QuotaExhausted(ApiError):
    """The API's quota is spent. Callers stop polling that API."""


class _Retryable(ApiError):
    """429/5xx, retried; becomes a plain ApiError once attempts run out."""


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), MAX_RETRY_AFTER)
    except ValueError:  # HTTP-date form: fall back to the backoff
        return None


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class RateLimitedClient:
    def __init__(self, base_url, headers=None, rate=2.0, timeout=20,
                 wait=wait_exponential_jitter(1, 8), transport=None):
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self._limiter = AsyncLimiter(rate, 1)
        self._wait = wait

    def _delay(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, ApiError) and exc.retry_after is not None:
            return exc.retry_after
        return self._wait(state)

    async def _once(self, path, params) -> httpx.Response:
        async with self._limiter:
            resp = await self._client.get(path, params=params)
        code = resp.status_code
        if code in (401, 403):
            raise AuthError(f"auth failed ({code}) for {path}", code)
        if code == 429 or code >= 500:
            raise _Retryable(f"HTTP {code} for {path}", code, _retry_after(resp))
        if code >= 400:
            raise ApiError(f"HTTP {code} for {path}", code)
        return resp

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=self._delay,
            retry=retry_if_exception_type(_Retryable),
            sleep=_sleep,
            reraise=True,
        )
        try:
            return await retrying(self._once, path, params)
        except _Retryable as e:
            raise ApiError(str(e), e.status, e.retry_after) from e

    async def aclose(self) -> None:
        await self._client.aclose()
