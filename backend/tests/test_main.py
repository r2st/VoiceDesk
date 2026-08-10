"""App factory: health checks and the startup/shutdown lifespan."""

from __future__ import annotations

from app.core import redis as redis_module
from app.main import lifespan


class TestHealth:
    async def test_health_reports_ok(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "voicedesk-api"

    async def test_readiness_is_healthy_when_db_and_redis_are_up(self, client):
        response = await client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"] == {"database": "ok", "redis": "ok"}

    async def test_readiness_degrades_when_redis_is_down(self, client, fake_redis, monkeypatch):
        async def broken_ping():
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(fake_redis, "ping", broken_ping)

        response = await client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["redis"].startswith("error: ConnectionError")
        assert body["checks"]["database"] == "ok"

    async def test_readiness_degrades_when_the_database_is_down(
        self, client, session, monkeypatch
    ):
        async def broken_execute(*args, **kwargs):
            raise RuntimeError("db unreachable")

        monkeypatch.setattr(session, "execute", broken_execute)

        response = await client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"].startswith("error: RuntimeError")


async def test_lifespan_starts_up_and_shuts_down_cleanly():
    from app.main import app as application

    async with lifespan(application):
        assert redis_module.get_redis() is not None
    # dispose_engine/close_redis ran without raising; the client is reset.
    assert redis_module._client is None
