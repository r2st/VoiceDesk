"""Phone number provisioning, binding and release (design doc §5.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, ExternalServiceError, ValidationError
from app.core.logging import get_logger, mask_phone
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.call import CallLog, PhoneNumber
from app.models.enums import CallStatus, PhoneNumberStatus
from app.models.voice_agent import VoiceAgent
from app.schemas.call import PhoneNumberUpdate, ProvisionNumberRequest
from app.services.telephony import get_provider

logger = get_logger(__name__)


async def provision_number(
    session: AsyncSession, business_id: uuid.UUID, payload: ProvisionNumberRequest
) -> PhoneNumber:
    """Allocate a number from the provider and bind it to the business.

    The row is written with ``PROVISIONING`` first so a provider failure still
    leaves an auditable record instead of vanishing.
    """
    if payload.agent_id is not None:
        await get_owned_or_404(session, VoiceAgent, payload.agent_id, business_id, label="Agent")

    provider_name = (payload.provider or settings.telephony_provider).lower()
    provider = get_provider(provider_name)

    if payload.number and await _already_held(session, payload.number, provider_name):
        raise ConflictError(f"Number {payload.number} is already provisioned.")

    number = PhoneNumber(
        business_id=business_id,
        agent_id=payload.agent_id,
        number=payload.number or "",
        provider=provider_name,
        region=payload.region,
        status=PhoneNumberStatus.PROVISIONING,
    )
    session.add(number)
    await session.flush()

    try:
        allocated = await provider.provision_number(region=payload.region, number=payload.number)
    except Exception as exc:
        number.status = PhoneNumberStatus.FAILED
        await session.flush()
        logger.warning("Provisioning failed for business %s: %s", business_id, exc)
        raise ExternalServiceError(
            f"Telephony provider could not allocate a number: {exc}"
        ) from exc

    # The provider chooses the number when the caller did not, and it can hand
    # back one the platform still holds — most often after a release the
    # provider recorded and this side did not. Reject it as a conflict rather
    # than letting the unique index surface as a 500.
    if await _already_held(session, allocated.number, provider_name, excluding=number.id):
        number.status = PhoneNumberStatus.FAILED
        await session.flush()
        raise ConflictError(f"Number {allocated.number} is already provisioned.")

    number.number = allocated.number
    number.provider_number_id = allocated.provider_number_id
    number.region = allocated.region or payload.region
    number.monthly_rent_paise = allocated.monthly_rent_paise
    number.status = PhoneNumberStatus.ACTIVE
    await session.flush()

    logger.info(
        "Provisioned %s for business %s (provider=%s)",
        mask_phone(number.number),
        business_id,
        provider_name,
    )
    return number


async def _already_held(
    session: AsyncSession,
    number: str,
    provider_name: str,
    *,
    excluding: uuid.UUID | None = None,
) -> bool:
    """Is this line already on the books, for any tenant?

    A number belongs to the network rather than to a business, so the check
    deliberately ignores tenant scoping — two tenants holding one line would
    route the same caller to two different agents.
    """
    stmt = select(PhoneNumber.id).where(
        PhoneNumber.number == number,
        PhoneNumber.provider == provider_name,
        PhoneNumber.deleted_at.is_(None),
    )
    if excluding is not None:
        stmt = stmt.where(PhoneNumber.id != excluding)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def get_number(
    session: AsyncSession, business_id: uuid.UUID, number_id: uuid.UUID
) -> PhoneNumber:
    return await get_owned_or_404(
        session, PhoneNumber, number_id, business_id, label="Phone number"
    )


async def list_numbers(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    status: PhoneNumberStatus | None = None,
    agent_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PhoneNumber], int]:
    stmt = tenant_select(PhoneNumber, business_id)
    if status is not None:
        stmt = stmt.where(PhoneNumber.status == status)
    if agent_id is not None:
        stmt = stmt.where(PhoneNumber.agent_id == agent_id)

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    result = await session.execute(
        stmt.order_by(PhoneNumber.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def update_number(
    session: AsyncSession,
    business_id: uuid.UUID,
    number_id: uuid.UUID,
    payload: PhoneNumberUpdate,
) -> PhoneNumber:
    """Rebind the number to another agent, or toggle inbound/outbound."""
    number = await get_number(session, business_id, number_id)
    data = payload.model_dump(exclude_unset=True)

    if "agent_id" in data and data["agent_id"] is not None:
        await get_owned_or_404(session, VoiceAgent, data["agent_id"], business_id, label="Agent")

    for field, value in data.items():
        setattr(number, field, value)
    await session.flush()
    return number


async def release_number(
    session: AsyncSession, business_id: uuid.UUID, number_id: uuid.UUID
) -> PhoneNumber:
    """Return the number to the provider and soft delete the row.

    Refused while calls are still live on the line — releasing underneath an
    in-progress call would drop it.
    """
    number = await get_number(session, business_id, number_id)

    live = await session.scalar(
        select(func.count())
        .select_from(CallLog)
        .where(
            CallLog.phone_number_id == number.id,
            CallLog.deleted_at.is_(None),
            CallLog.status.in_([CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]),
        )
    )
    if live:
        raise ConflictError(
            f"{live} call(s) are still active on this number; end them before releasing."
        )

    if number.provider_number_id:
        try:
            await get_provider(number.provider).release_number(number.provider_number_id)
        except Exception as exc:  # the provider may already have reclaimed it
            logger.warning("Provider release failed for %s: %s", number.id, exc)

    number.status = PhoneNumberStatus.RELEASED
    number.inbound_enabled = False
    number.outbound_enabled = False
    number.deleted_at = datetime.now(UTC)
    await session.flush()
    logger.info("Released %s for business %s", mask_phone(number.number), business_id)
    return number


async def assign_agent(
    session: AsyncSession,
    business_id: uuid.UUID,
    number_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> PhoneNumber:
    """Point an active line at an agent so inbound calls can be routed."""
    number = await get_number(session, business_id, number_id)
    if number.status != PhoneNumberStatus.ACTIVE:
        raise ValidationError(f"Number is {number.status}; only active numbers route calls.")
    await get_owned_or_404(session, VoiceAgent, agent_id, business_id, label="Agent")
    number.agent_id = agent_id
    await session.flush()
    return number


async def monthly_rent_paise(session: AsyncSession, business_id: uuid.UUID) -> int:
    """Total recurring line rent for the tenant, used by the billing meter."""
    total = await session.scalar(
        select(func.coalesce(func.sum(PhoneNumber.monthly_rent_paise), 0)).where(
            PhoneNumber.business_id == business_id,
            PhoneNumber.deleted_at.is_(None),
            PhoneNumber.status == PhoneNumberStatus.ACTIVE,
        )
    )
    return int(total or 0)
