"""Shared test fixtures.

The suite runs against in-memory SQLite for speed; ``app.db.types`` makes the
UUID and JSONB columns portable. Every external dependency (telephony, object
storage, WhatsApp, Redis, the LLM) is replaced with an in-process fake through
the ``set_*`` seams the services expose, so no test touches the network.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core import events as events_module
from app.core import redis as redis_module
from app.core.events import InMemoryEventBus
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db, set_sessionmaker
from app.main import create_app
from app.models.business import Business, User
from app.models.call import CallLog, PhoneNumber
from app.models.enums import (
    AgentStatus,
    BusinessStatus,
    CallDirection,
    CallStatus,
    Language,
    PhoneNumberStatus,
    PlanTier,
    TelephonyProvider,
    UserRole,
)
from app.models.voice_agent import VoiceAgent
from app.services import crm as crm_module
from app.services import llm as llm_module
from app.services import storage as storage_module
from app.services import telephony, whatsapp
from app.services.conversation_engine import set_engine
from app.services.flow import default_flow
from tests.fakes import (
    FakeCrmClient,
    FakeLLMClient,
    FakeRedis,
    FakeStorage,
    FakeWhatsAppProvider,
)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def engine():
    """A fresh in-memory database per test.

    ``StaticPool`` keeps every connection pointed at the same in-memory schema;
    without it each pooled connection would get its own empty database.
    """
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with maker() as s:
        # Code paths that cannot take a request-scoped session — the monitoring
        # WebSocket — resolve one through ``get_sessionmaker``. Point that at
        # this same session so they see the test's uncommitted fixture data
        # instead of opening a second, empty transaction.
        set_sessionmaker(_SharedSessionMaker(s))
        try:
            yield s
        finally:
            set_sessionmaker(None)


class _SharedSessionMaker:
    """Hands every caller the one session the test is already using."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _SharedSession:
        return _SharedSession(self._session)


class _SharedSession:
    """``async with`` wrapper that yields the shared session without closing it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


# --------------------------------------------------------------------------- #
# External dependency fakes
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def fake_redis() -> Iterator[FakeRedis]:
    client = FakeRedis()
    redis_module.set_redis(client)
    yield client
    redis_module.set_redis(None)


@pytest.fixture(autouse=True)
def mock_telephony() -> Iterator[telephony.MockTelephonyProvider]:
    provider = telephony.MockTelephonyProvider()
    telephony.set_provider_override(provider)
    yield provider
    telephony.reset_providers()


@pytest.fixture(autouse=True)
def fake_storage() -> Iterator[FakeStorage]:
    store = FakeStorage()
    storage_module.set_storage(store)
    yield store
    storage_module.set_storage(None)


@pytest.fixture(autouse=True)
def fake_whatsapp() -> Iterator[FakeWhatsAppProvider]:
    provider = FakeWhatsAppProvider()
    whatsapp.set_whatsapp_provider(provider)
    yield provider
    whatsapp.set_whatsapp_provider(None)


@pytest.fixture(autouse=True)
def fake_crm() -> Iterator[FakeCrmClient]:
    """In-process CRM endpoint. Tests assert against ``.pushed``."""
    client = FakeCrmClient()
    crm_module.set_crm_client(client)
    yield client
    crm_module.set_crm_client(None)


@pytest.fixture(autouse=True)
def event_bus() -> Iterator[InMemoryEventBus]:
    """In-process live-event bus. Tests assert against ``.published``."""
    bus = InMemoryEventBus()
    events_module.set_event_bus(bus)
    yield bus
    events_module.set_event_bus(None)


@pytest.fixture(autouse=True)
def fake_llm() -> Iterator[FakeLLMClient]:
    """Deterministic LLM. Tests that care about the reply set ``.replies``."""
    client = FakeLLMClient()
    llm_module.set_llm_client(client)
    yield client
    llm_module.set_llm_client(None)
    set_engine(None)


@pytest.fixture(autouse=True)
def inside_calling_hours(monkeypatch) -> None:
    """Pin "now" to 11:00 IST for the whole suite.

    Without this, any test that dials immediately would pass or fail depending
    on the wall-clock time of the run, because ``check_outbound_call`` enforces
    the 09:00–21:00 IST window. Tests that care about the window pass an
    explicit ``scheduled_at`` and are unaffected.
    """
    from zoneinfo import ZoneInfo

    from app.services import compliance

    pinned = datetime.now(ZoneInfo("Asia/Kolkata")).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    monkeypatch.setattr(compliance, "now_ist", lambda: pinned)


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch) -> Iterator[None]:
    """A fixed AES key so recording encryption is exercised, not skipped.

    ``_encryption_key`` is ``lru_cache``d, so the cache must be cleared on both
    sides of the override or the key from a previous test leaks into this one.
    """
    from app.core.config import settings
    from app.services.storage import _encryption_key

    monkeypatch.setattr(settings, "recording_encryption_key", "0" * 43 + "=", raising=False)
    _encryption_key.cache_clear()
    yield
    _encryption_key.cache_clear()


# --------------------------------------------------------------------------- #
# Tenants and fixtures data
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def business(session: AsyncSession) -> Business:
    row = Business(
        name="Sunrise Diagnostics",
        slug=f"sunrise-{uuid.uuid4().hex[:8]}",
        phone="+919876543210",
        email="owner@sunrise.test",
        industry="healthcare",
        city="Pune",
        state="Maharashtra",
        plan=PlanTier.GROWTH,
        status=BusinessStatus.ACTIVE,
        settings_json={},
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def other_business(session: AsyncSession) -> Business:
    """A second tenant — every isolation test asserts against this one."""
    row = Business(
        name="Rival Clinics",
        slug=f"rival-{uuid.uuid4().hex[:8]}",
        phone="+919812345678",
        email="owner@rival.test",
        plan=PlanTier.STARTER,
        status=BusinessStatus.ACTIVE,
        settings_json={},
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def owner(session: AsyncSession, business: Business) -> User:
    return await _make_user(session, business, "owner@sunrise.test", UserRole.OWNER)


@pytest_asyncio.fixture
async def viewer(session: AsyncSession, business: Business) -> User:
    return await _make_user(session, business, "viewer@sunrise.test", UserRole.VIEWER)


@pytest_asyncio.fixture
async def supervisor(session: AsyncSession, business: Business) -> User:
    return await _make_user(session, business, "super@sunrise.test", UserRole.SUPERVISOR)


@pytest_asyncio.fixture
async def second_supervisor(session: AsyncSession, business: Business) -> User:
    """A colleague — used to prove two people cannot hold the same call."""
    return await _make_user(session, business, "super2@sunrise.test", UserRole.SUPERVISOR)


@pytest_asyncio.fixture
async def other_owner(session: AsyncSession, other_business: Business) -> User:
    return await _make_user(session, other_business, "owner@rival.test", UserRole.OWNER)


async def _make_user(session: AsyncSession, business: Business, email: str, role: UserRole) -> User:
    user = User(
        business_id=business.id,
        email=email,
        full_name=email.split("@")[0].title(),
        password_hash=hash_password("Sup3rSecret!"),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def agent(session: AsyncSession, business: Business) -> VoiceAgent:
    row = VoiceAgent(
        business_id=business.id,
        name="Reception Agent",
        use_case="appointment_booking",
        status=AgentStatus.ACTIVE,
        language=Language.HINDI,
        supported_languages=[Language.HINDI, Language.ENGLISH],
        persona="You are a polite receptionist for a diagnostics clinic.",
        greeting="Namaste! Sunrise Diagnostics mein aapka swagat hai.",
        flow_json=default_flow("Namaste!", "appointment_booking"),
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def second_agent(session: AsyncSession, business: Business) -> VoiceAgent:
    """A second active agent on the same tenant — routing fallback tests."""
    row = VoiceAgent(
        business_id=business.id,
        name="Overflow Agent",
        use_case="customer_support",
        status=AgentStatus.ACTIVE,
        language=Language.HINDI,
        supported_languages=[Language.HINDI, Language.ENGLISH],
        persona="You are a backup receptionist for a diagnostics clinic.",
        greeting="Namaste! Sunrise Diagnostics mein aapka swagat hai.",
        flow_json=default_flow("Namaste!", "customer_support"),
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def phone_number(session: AsyncSession, business: Business, agent: VoiceAgent) -> PhoneNumber:
    row = PhoneNumber(
        business_id=business.id,
        agent_id=agent.id,
        number="+918000000001",
        provider=TelephonyProvider.MOCK,
        provider_number_id="mock-num-1",
        region="Maharashtra",
        status=PhoneNumberStatus.ACTIVE,
        monthly_rent_paise=15_000,
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def call(
    session: AsyncSession, business: Business, agent: VoiceAgent, phone_number: PhoneNumber
) -> CallLog:
    row = CallLog(
        business_id=business.id,
        agent_id=agent.id,
        phone_number_id=phone_number.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        caller_number="+919999988888",
        callee_number=phone_number.number,
        provider=TelephonyProvider.MOCK,
        provider_call_id="mock-call-1",
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        language=Language.HINDI,
        metadata_json={"state": {"turn_index": 0}},
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An ASGI client whose requests share the test's session and transaction.

    ``get_db`` is overridden rather than pointed at a second engine so data
    created by fixtures is visible to the request and vice versa.
    """
    application = create_app()

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    application.dependency_overrides.clear()


def auth_headers(user: User) -> dict[str, str]:
    """Bearer headers for a user, signed the same way the login endpoint signs."""
    token, _ = create_access_token(user_id=user.id, business_id=user.business_id, role=user.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner_headers(owner: User) -> dict[str, str]:
    return auth_headers(owner)


@pytest.fixture
def viewer_headers(viewer: User) -> dict[str, str]:
    return auth_headers(viewer)


@pytest.fixture
def supervisor_headers(supervisor: User) -> dict[str, str]:
    return auth_headers(supervisor)


@pytest.fixture
def other_headers(other_owner: User) -> dict[str, str]:
    return auth_headers(other_owner)
