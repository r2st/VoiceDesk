"""Account-standing gate on the call path.

Covers the module itself and the two places it is wired in, plus the loop that
gives it teeth: the trial-expiry job flips a tenant to ``suspended`` and the
gate must then refuse the call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import NotFoundError, QuotaExceededError
from app.models.enums import BusinessStatus, CallStatus
from app.schemas.call import InitiateCallRequest
from app.services import call_service, entitlements
from app.workers import jobs


class TestRequireCallingEntitlement:
    async def test_an_active_tenant_passes(self, session, business):
        assert await entitlements.require_calling_entitlement(session, business.id) is business

    async def test_a_trial_tenant_passes(self, session, business):
        """A running trial has to be able to make the calls that sell the product."""
        business.status = BusinessStatus.TRIAL
        business.trial_ends_at = datetime.now(UTC) + timedelta(days=7)
        await session.flush()

        assert await entitlements.require_calling_entitlement(session, business.id) is business

    async def test_a_suspended_tenant_is_refused(self, session, business):
        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        with pytest.raises(QuotaExceededError) as exc:
            await entitlements.require_calling_entitlement(session, business.id)
        assert exc.value.details["status"] == BusinessStatus.SUSPENDED

    async def test_a_cancelled_tenant_is_refused(self, session, business):
        business.status = BusinessStatus.CANCELLED
        await session.flush()

        with pytest.raises(QuotaExceededError):
            await entitlements.require_calling_entitlement(session, business.id)

    async def test_the_refusal_is_payment_required_not_forbidden(self, session, business):
        """402 is what routes the client to the upgrade screen."""
        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        with pytest.raises(QuotaExceededError) as exc:
            await entitlements.require_calling_entitlement(session, business.id)
        assert exc.value.status_code == 402
        assert exc.value.code == "quota_exceeded"

    async def test_a_soft_deleted_tenant_is_not_found(self, session, business):
        business.deleted_at = datetime.now(UTC)
        await session.flush()

        with pytest.raises(NotFoundError):
            await entitlements.require_calling_entitlement(session, business.id)

    async def test_an_unrecognised_status_does_not_block(self, session, business):
        """An unknown status must fail open — a bad enum value in the database
        should not silently take a paying tenant off the air."""
        business.status = "some_future_status"
        await session.flush()

        assert await entitlements.require_calling_entitlement(session, business.id) is business


class TestOutboundIsGated:
    async def test_a_suspended_tenant_cannot_place_a_call(self, session, business, agent):
        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        with pytest.raises(QuotaExceededError):
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="+919999911111"),
            )

    async def test_no_call_row_is_written_for_a_refused_tenant(
        self, session, business, agent, phone_number
    ):
        """Unlike a compliance block, an unentitled account leaves no call
        record — nothing was attempted on the tenant's behalf."""
        from sqlalchemy import select

        from app.models.call import CallLog

        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        with pytest.raises(QuotaExceededError):
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="+919999911111"),
            )

        rows = (await session.execute(select(CallLog))).scalars().all()
        assert rows == []

    async def test_an_active_tenant_still_places_calls(
        self, session, business, agent, phone_number
    ):
        call = await call_service.initiate_call(
            session,
            business.id,
            InitiateCallRequest(agent_id=agent.id, to_number="+919999911111"),
        )
        assert call.status != CallStatus.BLOCKED_DND


class TestInboundIsGated:
    async def test_a_suspended_tenant_does_not_answer_new_calls(
        self, session, business, phone_number
    ):
        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        with pytest.raises(QuotaExceededError):
            await call_service.handle_inbound_call(
                session,
                to_number=phone_number.number,
                from_number="+919999922222",
                provider_call_id="inbound-suspended-1",
                provider="mock",
            )

    async def test_a_call_already_in_progress_survives_a_mid_call_suspension(
        self, session, business, phone_number
    ):
        """A webhook replay for an accepted call must keep working, or the
        tenant's live call breaks the moment its trial lapses."""
        call, _ = await call_service.handle_inbound_call(
            session,
            to_number=phone_number.number,
            from_number="+919999933333",
            provider_call_id="inbound-inflight-1",
            provider="mock",
        )

        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        replayed, _ = await call_service.handle_inbound_call(
            session,
            to_number=phone_number.number,
            from_number="+919999933333",
            provider_call_id="inbound-inflight-1",
            provider="mock",
        )
        assert replayed.id == call.id


class TestTrialExpiryTakesEffect:
    async def test_an_expired_trial_stops_calling(self, session, business, agent, phone_number):
        """The end-to-end loop: the job suspends, the gate refuses."""
        business.status = BusinessStatus.TRIAL
        business.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

        # Before the job runs, the lapsed trial can still dial.
        await entitlements.require_calling_entitlement(session, business.id)

        assert (await jobs.expire_trials(session)).detail["suspended"] == 1

        with pytest.raises(QuotaExceededError):
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="+919999944444"),
            )
