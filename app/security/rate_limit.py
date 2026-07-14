"""Redis sliding-window rate limiting (§7.6).

A sliding window, not a fixed one: a fixed window lets a caller send the full quota in the last
second of one window and the full quota in the first second of the next — twice the intended rate
across that boundary. The sliding window counts the *preceding* 60 seconds from now, so the limit
holds continuously.

Implemented as a Redis sorted set keyed by scope+identity, with timestamps as scores:
  1. drop entries older than the window
  2. count what remains
  3. if under the limit, add this request
All four steps run in one pipeline so two concurrent requests cannot both observe "under the
limit" and both be admitted.

**Fail-open, deliberately.** If Redis is unreachable the request is allowed. A rate limiter is a
guardrail, not an authentication boundary — a Redis outage must degrade throughput protection, not
lock every user out of a government platform mid-incident. The failure is logged loudly.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import Settings, get_settings
from app.logging import get_request_id
from app.metrics import RATE_LIMITED

log = structlog.get_logger(__name__)

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Redis | None = None

    def _connect(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(
                self._redis_url, socket_connect_timeout=2, socket_timeout=2
            )
        return self._client

    async def check(self, *, scope: str, identity: str, limit: int) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        if limit <= 0:
            return True, 0

        key = f"ratelimit:{scope}:{identity}"
        now = time.time()
        cutoff = now - WINDOW_SECONDS

        try:
            client = self._connect()
            async with client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, cutoff)
                pipe.zcard(key)
                # The member must be unique per request or ZADD updates the existing entry
                # instead of adding one, and the window silently under-counts. Neither the
                # clock nor the request id can be relied on for that: time.time() is coarse
                # (~15ms on Windows), so a burst lands on one timestamp, and the id is "-"
                # outside a request context. A burst is exactly when the limit must hold.
                pipe.zadd(key, {f"{now}:{uuid4().hex}": now})
                pipe.expire(key, WINDOW_SECONDS)
                results = await pipe.execute()

            count = int(results[1])
        except (RedisError, OSError) as exc:
            # Fail open: a rate limiter outage must not become an authentication outage.
            log.error("rate_limit_unavailable", scope=scope, error=str(exc))
            return True, 0

        if count >= limit:
            # The oldest request in the window governs when a slot frees up.
            try:
                oldest = await self._connect().zrange(key, 0, 0, withscores=True)
                retry_after = (
                    max(1, int(WINDOW_SECONDS - (now - oldest[0][1]))) if oldest else WINDOW_SECONDS
                )
            except (RedisError, OSError):
                retry_after = WINDOW_SECONDS
            return False, retry_after

        return True, 0

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_limiter: RateLimiter | None = None


def get_limiter(settings: Settings | None = None) -> RateLimiter:
    global _limiter
    if _limiter is None:
        cfg = settings or get_settings()
        _limiter = RateLimiter(cfg.redis_url)
    return _limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies the limits from §7.6: login 5/min/IP, chat 20/min/user.

    Login is limited by IP because there is no user yet — that is the whole point of limiting it
    (credential stuffing arrives with a different email each time). Chat is limited by user id,
    because limiting an entire ministry building by its shared egress IP would punish everyone for
    one enthusiastic analyst.
    """

    def __init__(self, app: object, settings: Settings | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.settings = settings or get_settings()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rule = self._rule_for(request)
        if rule is None:
            return await call_next(request)

        scope, identity, limit = rule
        limiter = get_limiter(self.settings)
        allowed, retry_after = await limiter.check(scope=scope, identity=identity, limit=limit)

        if not allowed:
            RATE_LIMITED.labels(scope=scope).inc()
            log.warning("rate_limited", scope=scope, limit=limit, retry_after=retry_after)
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": (
                            f"Too many requests. The limit is {limit} per minute. "
                            f"Retry in {retry_after} seconds."
                        ),
                        "request_id": get_request_id(),
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _rule_for(self, request: Request) -> tuple[str, str, int] | None:
        path = request.url.path

        if request.method == "POST" and path == "/api/auth/login":
            return "login", self._client_ip(request), self.settings.rate_limit_login_per_minute

        if request.method == "POST" and path == "/api/agent/chat":
            # Identify by the bearer token rather than decoding the JWT here: this middleware runs
            # before dependency resolution, so there is no authenticated user yet. The token is a
            # stable per-user identifier for the window's lifetime, which is all that is needed.
            auth = request.headers.get("Authorization", "")
            identity = auth[-32:] if auth else self._client_ip(request)
            return "chat", identity, self.settings.rate_limit_chat_per_minute

        return None

    @staticmethod
    def _client_ip(request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
