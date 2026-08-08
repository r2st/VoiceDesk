"""Shared async Redis client."""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _client


def set_redis(client: aioredis.Redis | None) -> None:
    """Override the shared client (used by tests to inject a fake)."""
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
