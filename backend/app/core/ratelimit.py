"""Per-tenant sliding-window rate limiting (design doc §5.1: 100 req/min).

Backed by Redis so the limit holds across worker processes. If Redis is
unavailable the limiter fails open — a cache outage must not take the API down.
"""

from __future__ import annotations

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.core.security import decode_token

logger = get_logger(__name__)

#: Paths that must stay reachable regardless of tenant quota.
EXEMPT_PATHS = frozenset({"/health", "/health/ready", "/docs", "/openapi.json", "/redoc"})


class RateLimiter:
    """Fixed-window counter keyed by tenant (or client IP when unauthenticated)."""

    def __init__(self, limit: int | None = None, window_seconds: int = 60) -> None:
        self.limit = limit or settings.rate_limit_per_minute
        self.window_seconds = window_seconds

    async def check(self, key: str) -> tuple[bool, int, int]:
        """Return ``(allowed, remaining, reset_epoch)``."""
        window = int(time.time()) // self.window_seconds
        reset_at = (window + 1) * self.window_seconds
        redis_key = f"ratelimit:{key}:{window}"
        try:
            client = get_redis()
            count = await client.incr(redis_key)
            if count == 1:
                await client.expire(redis_key, self.window_seconds)
        except Exception as exc:  # pragma: no cover - depends on Redis being down
            logger.warning("Rate limiter unavailable, allowing request: %s", exc)
            return True, self.limit, reset_at
        return count <= self.limit, max(0, self.limit - count), reset_at


def _identify(request: Request) -> str:
    """Prefer the tenant from the bearer token; fall back to the client IP."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            payload = decode_token(auth.split(" ", 1)[1], expected_type="access")
            return f"business:{payload['business_id']}"
        except Exception:
            pass
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        self.limiter = limiter or RateLimiter()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in EXEMPT_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        key = _identify(request)
        allowed, remaining, reset_at = await self.limiter.check(key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Rate limit exceeded. Try again shortly.",
                    }
                },
                headers={
                    "Retry-After": str(max(1, reset_at - int(time.time()))),
                    "X-RateLimit-Limit": str(self.limiter.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_at),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_at)
        return response
