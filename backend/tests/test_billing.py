"""Usage metering and billing cycles. All money is integer paise (§6.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.analytics import BillingUsage
from app.models.call import CallLog
from app.models.enums import CallDirection, CallResolution, CallStatus, PlanTier
from app.services import billing_service
from app.services.plans import PLANS, apply_gst, get_plan, paise_to_rupees


class TestBillableMinutes:
    @pytest.mark.parametrize(
        ("seconds", "minutes"),
        [(0, 0), (-5, 0), (1, 1), (59, 1), (60, 1), (61, 2), (90, 2), (600, 10), (601, 11)],
    )
    def test_rounds_up_to_the_started_minute(self, seconds, minutes):
        assert billing_service.billable_minutes(seconds) == minutes


class TestCurrentMonth:
    def test_formats_as_year_month(self):
        assert billing_service.current_month(datetime(2026, 8, 8, tzinfo=UTC)) == "2026-08"

    def test_uses_ist_not_utc(self):
        """23:30 UTC on the 31st is already the 1st in IST — a new billing cycle."""
        late = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
        assert billing_service.current_month(late) == "2026-09"


class TestMeterCall:
    async def test_completed_call_is_metered_at_the_plan_rate(
        self, session, business, call
    ):
        business.plan = PlanTier.GROWTH
        call.status = CallStatus.COMPLETED
        call.duration_sec = 125
        await session.flush()

        result = await billing_service.meter_call(session, call)

        plan = get_plan(PlanTier.GROWTH)
        assert result.minutes == 3  # 125s -> 3 started minutes
        assert result.cost_paise == 3 * plan.per_minute_paise
        assert call.billable_minutes == 3

    @pytest.mark.parametrize(
        "status", [CallStatus.NO_ANSWER, CallStatus.BUSY, CallStatus.FAILED, CallStatus.BLOCKED_DND]
    )
    async def test_unconnected_calls_are_free(self, session, business, call, status):
        call.status = status
        call.duration_sec = 30
        await session.flush()

        result = await billing_service.meter_call(session, call)
        assert (result.minutes, result.cost_paise) == (0, 0)
        assert call.cost_paise == 0

    async def test_metering_is_idempotent(self, session, business, call):
        """A replayed webhook must not double-charge."""
        call.status = CallStatus.COMPLETED
        call.duration_sec = 125
        await session.flush()

        first = await billing_service.meter_call(session, call)
        second = await billing_service.meter_call(session, call)

        assert first == second
        assert call.billable_minutes == 3

    async def test_rate_follows_the_business_plan(self, session, business, call):
        call.status = CallStatus.COMPLETED
        call.duration_sec = 60
        business.plan = PlanTier.STARTER
        await session.flush()
        starter = await billing_service.meter_call(session, call)

        # Re-meter under a cheaper plan; Business tier bills less per minute.
        business.plan = PlanTier.BUSINESS
        await session.flush()
        business_tier = await billing_service.meter_call(session, call)

        assert business_tier.cost_paise < starter.cost_paise


class TestUsageCycle:
    async def test_creates_a_cycle_seeded_from_the_plan(self, session, business):
        business.plan = PlanTier.GROWTH
        await session.flush()

        usage = await billing_service.get_or_create_usage(session, business.id, "2026-08")

        plan = get_plan(PlanTier.GROWTH)
        assert usage.month == "2026-08"
        assert usage.included_minutes == plan.included_minutes
        assert usage.base_fee_paise == plan.monthly_fee_paise

    async def test_is_idempotent(self, session, business):
        first = await billing_service.get_or_create_usage(session, business.id, "2026-08")
        second = await billing_service.get_or_create_usage(session, business.id, "2026-08")
        assert first.id == second.id

    async def test_recalculate_totals_from_call_rows(self, session, business, agent):
        business.plan = PlanTier.GROWTH
        await session.flush()

        month_start = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
        for minutes in (2, 3):
            session.add(
                CallLog(
                    business_id=business.id,
                    agent_id=agent.id,
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.COMPLETED,
                    caller_number="+919999900000",
                    callee_number="+918000000001",
                    duration_sec=minutes * 60,
                    billable_minutes=minutes,
                    cost_paise=minutes * get_plan(PlanTier.GROWTH).per_minute_paise,
                    created_at=month_start,
                )
            )
        await session.flush()

        usage = await billing_service.recalculate_usage(session, business.id, "2026-08")

        assert usage.minutes_used == 5
        assert usage.calls_count == 2
        assert usage.total_paise > 0

    async def test_overage_is_charged_beyond_included_minutes(
        self, session, business, agent
    ):
        business.plan = PlanTier.STARTER  # 200 included minutes
        await session.flush()
        plan = get_plan(PlanTier.STARTER)

        session.add(
            CallLog(
                business_id=business.id,
                agent_id=agent.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                caller_number="+919999900000",
                callee_number="+918000000001",
                duration_sec=250 * 60,
                billable_minutes=250,
                cost_paise=0,
                created_at=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
            )
        )
        await session.flush()

        usage = await billing_service.recalculate_usage(session, business.id, "2026-08")

        assert usage.overage_minutes == 50
        assert usage.overage_paise == 50 * plan.per_minute_paise

    async def test_no_overage_within_the_allowance(self, session, business, agent):
        business.plan = PlanTier.STARTER
        await session.flush()

        session.add(
            CallLog(
                business_id=business.id,
                agent_id=agent.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                caller_number="+919999900000",
                callee_number="+918000000001",
                duration_sec=60 * 60,
                billable_minutes=60,
                created_at=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
            )
        )
        await session.flush()

        usage = await billing_service.recalculate_usage(session, business.id, "2026-08")
        assert usage.overage_minutes == 0 and usage.overage_paise == 0

    async def test_calls_outside_the_cycle_are_excluded(self, session, business, agent):
        session.add(
            CallLog(
                business_id=business.id,
                agent_id=agent.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                caller_number="+919999900000",
                callee_number="+918000000001",
                duration_sec=600,
                billable_minutes=10,
                created_at=datetime(2026, 7, 15, 6, 0, tzinfo=UTC),
            )
        )
        await session.flush()

        usage = await billing_service.recalculate_usage(session, business.id, "2026-08")
        assert usage.minutes_used == 0

    async def test_another_tenants_calls_are_excluded(
        self, session, business, other_business
    ):
        session.add(
            CallLog(
                business_id=other_business.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                caller_number="+919999900000",
                callee_number="+918000000001",
                duration_sec=600,
                billable_minutes=10,
                created_at=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
            )
        )
        await session.flush()

        usage = await billing_service.recalculate_usage(session, business.id, "2026-08")
        assert usage.minutes_used == 0


class TestFinalize:
    async def test_closes_the_cycle_and_assigns_an_invoice_number(
        self, session, business
    ):
        await billing_service.get_or_create_usage(session, business.id, "2026-07")
        usage = await billing_service.finalize_month(session, business.id, "2026-07")

        assert usage.is_finalized is True
        assert usage.finalized_at is not None
        assert usage.invoice_number

    async def test_is_idempotent(self, session, business):
        await billing_service.get_or_create_usage(session, business.id, "2026-07")
        first = await billing_service.finalize_month(session, business.id, "2026-07")
        invoice = first.invoice_number
        second = await billing_service.finalize_month(session, business.id, "2026-07")

        assert second.invoice_number == invoice


class TestQuota:
    async def test_reports_remaining_minutes(self, session, business):
        business.plan = PlanTier.GROWTH
        await session.flush()

        status = await billing_service.quota_status(session, business.id, "2026-08")

        plan = get_plan(PlanTier.GROWTH)
        assert status["remaining_minutes"] == plan.included_minutes
        assert status["in_overage"] is False
        assert status["utilization_pct"] == 0.0


class TestPlans:
    def test_every_tier_is_defined(self):
        assert {t.value for t in PlanTier} == {p.value for p in PLANS}

    def test_higher_tiers_cost_less_per_minute(self):
        rates = [PLANS[t].per_minute_paise for t in (PlanTier.STARTER, PlanTier.GROWTH, PlanTier.BUSINESS)]
        assert rates == sorted(rates, reverse=True)

    def test_higher_tiers_include_more_minutes(self):
        included = [
            PLANS[t].included_minutes
            for t in (PlanTier.STARTER, PlanTier.GROWTH, PlanTier.BUSINESS)
        ]
        assert included == sorted(included)

    def test_annual_price_applies_the_twenty_percent_discount(self):
        plan = get_plan(PlanTier.GROWTH)
        assert plan.annual_fee_paise() == plan.monthly_fee_paise * 12 * 80 // 100

    def test_enterprise_is_custom_priced(self):
        assert get_plan(PlanTier.ENTERPRISE).is_custom

    def test_business_tier_has_no_agent_or_language_cap(self):
        plan = get_plan(PlanTier.BUSINESS)
        assert plan.max_agents is None and plan.max_languages is None

    def test_unknown_plan_falls_back_rather_than_crashing(self):
        assert get_plan("nonexistent-tier").tier == PlanTier.STARTER

    def test_gst_is_eighteen_percent(self):
        assert apply_gst(10_000) == 1_800

    def test_paise_render_as_rupees(self):
        assert paise_to_rupees(399_900) == "3999.00"


class TestBillingEndpoints:
    async def test_usage_endpoint_returns_plan_and_quota(self, client, owner_headers):
        response = await client.get("/api/v1/billing/usage", headers=owner_headers)
        body = response.json()

        assert response.status_code == 200
        assert body["plan"]["tier"] == PlanTier.GROWTH
        assert body["quota"]["included_minutes"] == get_plan(PlanTier.GROWTH).included_minutes
        assert "projected_total_paise" in body

    async def test_month_must_be_well_formed(self, client, owner_headers):
        response = await client.get("/api/v1/billing/usage?month=August", headers=owner_headers)
        assert response.status_code == 422
        assert "YYYY-MM" in response.json()["error"]["message"]

    async def test_plans_catalogue_is_public_to_any_authenticated_user(
        self, client, viewer_headers
    ):
        response = await client.get("/api/v1/billing/plans", headers=viewer_headers)
        assert response.status_code == 200
        assert len(response.json()) == 4

    async def test_history_is_scoped_to_the_tenant(
        self, client, session, owner_headers, business, other_business
    ):
        session.add_all(
            [
                BillingUsage(business_id=business.id, month="2026-07"),
                BillingUsage(business_id=other_business.id, month="2026-07"),
            ]
        )
        await session.flush()

        response = await client.get("/api/v1/billing/history", headers=owner_headers)
        assert len(response.json()) == 1

    async def test_finalize_requires_admin(self, client, viewer_headers):
        response = await client.post(
            "/api/v1/billing/finalize?month=2026-07", headers=viewer_headers
        )
        assert response.status_code == 403

    async def test_quota_endpoint(self, client, owner_headers):
        response = await client.get("/api/v1/billing/quota", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["in_overage"] is False
