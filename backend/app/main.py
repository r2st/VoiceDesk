"""VoiceDesk API application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import RateLimitMiddleware
from app.core.redis import close_redis, get_redis
from app.db.session import dispose_engine, get_sessionmaker

logger = get_logger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("VoiceDesk API starting (env=%s)", settings.voicedesk_env)
    yield
    await close_redis()
    await dispose_engine()
    logger.info("VoiceDesk API stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="VoiceDesk API",
        description="AI Voice Agent Platform for Indian Businesses",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    )
    app.add_middleware(RateLimitMiddleware)

    register_exception_handlers(app)
    _register_routers(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok", "service": "voicedesk-api", "version": app.version}

    @app.get("/health/ready", tags=["health"])
    async def readiness() -> dict:
        checks = {"database": "unknown", "redis": "unknown"}
        try:
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
        try:
            await get_redis().ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"
        healthy = all(v == "ok" for v in checks.values())
        return {"status": "ok" if healthy else "degraded", "checks": checks}

    return app


def _register_routers(app: FastAPI) -> None:
    from app.api.v1 import (
        agents,
        analytics,
        appointments,
        billing,
        calls,
        intents,
        monitor,
        phone_numbers,
        recordings,
        webhooks,
        whatsapp,
    )
    from app.api.v1 import auth as auth_router

    for module in (
        auth_router,
        agents,
        intents,
        phone_numbers,
        appointments,
        calls,
        recordings,
        monitor,
        whatsapp,
        analytics,
        billing,
        webhooks,
    ):
        app.include_router(module.router, prefix=API_PREFIX)


app = create_app()
