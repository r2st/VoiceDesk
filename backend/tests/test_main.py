"""App factory: health checks and the startup/shutdown lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from app.core import redis as redis_module
from app.core.config import settings
from app.db.session import get_db
from app.main import create_app, lifespan


@asynccontextmanager
async def production_client(session) -> AsyncIterator[AsyncClient]:
    """A client against a freshly built app, so it picks up a patched env.

    The shared ``client`` fixture builds its app before a test can change
    ``voicedesk_env``, and ``create_app`` reads that once at construction.
    """
    application = create_app()

    async def _override_get_db():
        yield session

    application.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    application.dependency_overrides.clear()


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


class TestSchemaExposure:
    """The OpenAPI schema maps every endpoint, its roles and its payloads.

    The dashboard is the only client and it is written against the schema at
    build time, so serving it at runtime gives an attacker a free inventory
    and gives legitimate users nothing.
    """

    async def test_docs_are_available_outside_production(self, client: AsyncClient):
        assert (await client.get("/openapi.json")).status_code == 200
        assert (await client.get("/docs")).status_code == 200

    def test_production_serves_no_schema_or_docs_ui(self, monkeypatch):
        monkeypatch.setattr(settings, "voicedesk_env", "production")
        application = create_app()

        assert application.docs_url is None
        assert application.redoc_url is None
        assert application.openapi_url is None

    async def test_production_docs_routes_are_404(self, monkeypatch, session):
        monkeypatch.setattr(settings, "voicedesk_env", "production")
        async with production_client(session) as http:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert (await http.get(path)).status_code == 404

    async def test_health_still_answers_in_production(self, monkeypatch, session):
        """Locking down the schema must not take the load balancer probe with it."""
        monkeypatch.setattr(settings, "voicedesk_env", "production")
        async with production_client(session) as http:
            assert (await http.get("/health")).status_code == 200


async def test_lifespan_starts_up_and_shuts_down_cleanly():
    from app.main import app as application

    async with lifespan(application):
        assert redis_module.get_redis() is not None
    # dispose_engine/close_redis ran without raising; the client is reset.
    assert redis_module._client is None
