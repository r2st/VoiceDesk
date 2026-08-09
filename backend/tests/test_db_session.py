"""The engine/sessionmaker factory that backs ``get_db`` in production.

Every other test in the suite runs through ``set_sessionmaker`` overrides, so
this module is the only place ``get_engine``/``get_sessionmaker``/``get_db``
themselves get exercised. Each test points ``settings.database_url`` at a
throwaway in-memory SQLite database and disposes the engine afterwards so it
can't leak into other tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db import session as db_session


async def _reset():
    await db_session.dispose_engine()


class TestGetEngine:
    async def test_creates_and_caches_a_single_engine(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
        await _reset()
        try:
            first = db_session.get_engine()
            second = db_session.get_engine()
            assert first is second
        finally:
            await _reset()


class TestGetSessionmaker:
    async def test_creates_and_caches_a_single_sessionmaker(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
        await _reset()
        try:
            first = db_session.get_sessionmaker()
            second = db_session.get_sessionmaker()
            assert first is second
        finally:
            await _reset()


class TestGetDb:
    async def test_yields_a_session_and_commits_on_success(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
        await _reset()
        try:
            async for session in db_session.get_db():
                result = await session.execute(text("SELECT 1"))
                assert result.scalar_one() == 1
        finally:
            await _reset()

    async def test_rolls_back_and_reraises_on_failure(self, monkeypatch):
        """Mirrors how FastAPI drives a dependency generator: it throws the
        endpoint's exception back into the generator at the yield point
        (via ``athrow``) rather than merely closing it, which is what lets
        the ``except Exception`` branch below actually run in production."""
        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
        await _reset()
        try:
            gen = db_session.get_db()
            await gen.__anext__()
            with pytest.raises(RuntimeError, match="boom"):
                await gen.athrow(RuntimeError("boom"))
        finally:
            await _reset()


class TestDisposeEngine:
    async def test_resets_both_globals_and_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
        await _reset()
        db_session.get_engine()
        db_session.get_sessionmaker()

        await db_session.dispose_engine()
        assert db_session._engine is None
        assert db_session._sessionmaker is None

        # Disposing again with nothing to dispose must not raise.
        await db_session.dispose_engine()
