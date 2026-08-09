"""BANT scoring, lead capture, the pipeline API, the flow node and CRM push.

Scoring is a pure function, so most of it is tested without a database: the
value of that purity is that a disputed score can be reproduced from the four
sentences alone, and these tests hold it to that.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.models.business import Business
from app.models.call import CallLog
from app.models.enums import (
    BANTDimension,
    CrmPushStatus,
    LeadSource,
    LeadStatus,
    LeadTier,
)
from app.models.lead import Lead
from app.models.voice_agent import VoiceAgent
from app.schemas.lead import BantAnswersIn, LeadCreate, LeadUpdate, QualificationWeightsUpdate
from app.services import bant, crm, lead_service
from app.services.bant import BantAnswers
from app.workers import jobs
from tests.fakes import FakeCrmClient

#: A caller who ticks every box. Used wherever a test needs a lead that is
#: unambiguously hot so the assertion is about something else.
STRONG = BantAnswers(
    budget="we have approved a budget of 5 lakh for this",
    authority="I am the owner, I decide",
    need="our current system is losing us orders, it is urgent",
    timeline="we want to start immediately",
)

WEAK = BantAnswers(
    budget="not sure, have to check",
    authority="my manager will have to approve",
    need="just looking around for now",
    timeline="no rush, sometime next year",
)


def make_lead_payload(**overrides) -> LeadCreate:
    data = {
        "contact_name": "Rohit Sharma",
        "contact_phone": "9876500022",
        "answers": BantAnswersIn(
            budget=STRONG.budget,
            authority=STRONG.authority,
            need=STRONG.need,
            timeline=STRONG.timeline,
        ),
    }
    data.update(overrides)
    return LeadCreate(**data)


# --------------------------------------------------------------------------- #
# Amount parsing
# --------------------------------------------------------------------------- #
class TestParseAmount:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("we can spend 2 lakh", 200_000),
            ("budget is ₹50,000", 50_000),
            ("about Rs. 1.5 crore", 15_000_000),
            ("around 80k", 80_000),
            ("INR 2,00,000 roughly", 200_000),
            ("maybe 5 lakhs", 500_000),
        ],
    )
    def test_spoken_figures_are_understood(self, text: str, expected: float):
        assert bant.parse_amount(text) == expected

    @pytest.mark.parametrize("text", ["no idea", "", "we'll see"])
    def test_text_without_a_figure_yields_nothing(self, text: str):
        assert bant.parse_amount(text) is None

    def test_a_bare_number_without_a_unit_is_not_a_budget(self):
        """ "Call me on 9876543210" must not read as a ₹9.8bn budget."""
        assert bant.parse_amount("call me on 9876543210") is None


# --------------------------------------------------------------------------- #
# Per-dimension scoring
# --------------------------------------------------------------------------- #
class TestDimensionScoring:
    def test_a_stated_figure_at_the_floor_scores_full_marks(self):
        scored = bant.score_budget("we have 50,000 set aside", floor=50_000)
        assert scored.score == 1.0
        assert "50,000" in scored.reason

    def test_a_small_figure_still_beats_no_figure(self):
        """A caller who names ₹5,000 has thought about money; a vague one has not."""
        small = bant.score_budget("about 5000 rupees", floor=50_000)
        vague = bant.score_budget("we would have to look into it", floor=50_000)
        assert small.score == 0.4
        assert small.score >= vague.score

    def test_a_missing_answer_scores_zero(self):
        for scorer in (
            lambda: bant.score_budget(None, floor=50_000),
            lambda: bant.score_authority("   "),
            lambda: bant.score_need(None),
            lambda: bant.score_timeline(""),
        ):
            assert scorer().score == 0.0

    def test_a_negated_budget_does_not_score_as_confirmed(self):
        """ "budget is not approved" contains "approved" but means the opposite."""
        assert bant.score_budget("the budget is not approved yet", floor=50_000).score == 0.2

    def test_hindi_answers_are_scored_like_english_ones(self):
        assert bant.score_authority("मैं ही मालिक हूँ").score == 1.0
        assert bant.score_need("हमें तुरंत ज़रूरत है").score == 1.0
        assert bant.score_budget("हाँ, बजट है", floor=50_000).score == 0.8

    def test_romanised_hindi_is_scored_too(self):
        """Callers code-switch; "boss se puchna padega" is a real answer."""
        assert bant.score_authority("boss se puchna padega").score == 0.25
        assert bant.score_need("humein bahut zaroori hai").score == 1.0

    def test_a_shared_decision_scores_between_owner_and_gatekeeper(self):
        owner = bant.score_authority("I am the owner").score
        shared = bant.score_authority("we decide jointly with my partner").score
        gatekeeper = bant.score_authority("my boss has to approve").score
        assert owner > shared > gatekeeper

    def test_the_tightest_timeline_band_wins(self):
        assert bant.score_timeline("we want to start immediately").score == 1.0
        assert bant.score_timeline("sometime next year").score == 0.25
        assert bant.score_timeline("next month maybe").score == 0.5

    def test_a_described_need_beats_a_one_word_one(self):
        detailed = bant.score_need("our booking system keeps dropping customer calls")
        terse = bant.score_need("software")
        assert detailed.score > terse.score


# --------------------------------------------------------------------------- #
# Whole-lead scoring
# --------------------------------------------------------------------------- #
class TestQualify:
    def test_a_strong_caller_is_hot_and_qualified(self):
        result = bant.qualify(STRONG, bant.load_qualification_config({}))
        assert result.tier is LeadTier.HOT
        assert result.qualified is True
        assert result.score >= 80

    def test_a_browsing_caller_is_cold_and_unqualified(self):
        result = bant.qualify(WEAK, bant.load_qualification_config({}))
        assert result.tier is LeadTier.COLD
        assert result.qualified is False

    def test_a_silent_caller_is_unqualified_rather_than_cold(self):
        """Nothing said is a different state from everything said badly."""
        result = bant.qualify(BantAnswers(), bant.load_qualification_config({}))
        assert result.score == 0
        assert result.tier is LeadTier.UNQUALIFIED

    def test_every_dimension_carries_a_reason(self):
        result = bant.qualify(STRONG, bant.load_qualification_config({}))
        assert set(result.rationale()) == {d.value for d in BANTDimension}
        assert all(reason for reason in result.rationale().values())

    def test_weights_move_the_score(self):
        """A tenant that only cares about timing scores a punctual browser well."""
        answers = BantAnswers(
            budget="not sure",
            authority="my manager decides",
            need="just looking",
            timeline="we want to start today",
        )
        balanced = bant.qualify(answers, bant.load_qualification_config({}))
        timing_only = bant.qualify(
            answers,
            bant.load_qualification_config(
                {
                    "lead_qualification": {
                        "weights": {"budget": 0, "authority": 0, "need": 0, "timeline": 100}
                    }
                }
            ),
        )
        assert timing_only.score > balanced.score
        assert timing_only.score == 100

    def test_scoring_is_deterministic(self):
        config = bant.load_qualification_config({})
        assert bant.qualify(STRONG, config).score == bant.qualify(STRONG, config).score


class TestQualificationConfig:
    def test_defaults_apply_when_nothing_is_configured(self):
        config = bant.load_qualification_config(None)
        assert config.weights == bant.DEFAULT_WEIGHTS
        assert config.hot_at == bant.DEFAULT_HOT_AT
        assert config.total_weight == 100

    def test_a_malformed_block_falls_back_instead_of_raising(self):
        """A bad value saved months ago must not fail a live call."""
        config = bant.load_qualification_config(
            {"lead_qualification": {"weights": "nonsense", "hot_at": "soon", "qualify_at": None}}
        )
        assert config.weights == bant.DEFAULT_WEIGHTS
        assert config.hot_at == bant.DEFAULT_HOT_AT

    def test_all_weights_zero_falls_back_rather_than_dividing_by_nothing(self):
        config = bant.load_qualification_config(
            {
                "lead_qualification": {
                    "weights": {"budget": 0, "authority": 0, "need": 0, "timeline": 0}
                }
            }
        )
        assert config.total_weight == 100

    def test_a_warm_threshold_above_hot_is_clamped(self):
        """Otherwise there would be a band that is both warm and hot."""
        config = bant.load_qualification_config(
            {"lead_qualification": {"hot_at": 70, "warm_at": 90}}
        )
        assert config.warm_at == 70

    def test_out_of_range_thresholds_are_bounded(self):
        config = bant.load_qualification_config(
            {"lead_qualification": {"hot_at": 900, "qualify_at": -5}}
        )
        assert config.hot_at == 100
        assert config.qualify_at == 1


# --------------------------------------------------------------------------- #
# Capture and rescoring
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestCapture:
    async def test_a_captured_lead_stores_answers_scores_and_reasons(
        self, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        assert lead.status == LeadStatus.QUALIFIED
        assert lead.tier == LeadTier.HOT
        assert lead.budget_answer == STRONG.budget
        assert lead.need_score == 1.0
        assert lead.rationale["authority"]
        assert lead.qualified_at is not None

    async def test_an_unqualified_lead_is_marked_disqualified(
        self, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(
            session,
            business.id,
            make_lead_payload(
                answers=BantAnswersIn(
                    budget=WEAK.budget,
                    authority=WEAK.authority,
                    need=WEAK.need,
                    timeline=WEAK.timeline,
                )
            ),
        )
        assert lead.status == LeadStatus.DISQUALIFIED
        assert lead.qualified_at is None

    async def test_the_phone_number_is_normalised_on_capture(
        self, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(
            session, business.id, make_lead_payload(contact_phone="98765 00022")
        )
        assert lead.contact_phone == "+919876500022"

    async def test_rescoring_picks_up_new_weights(self, session: AsyncSession, business: Business):
        lead = await lead_service.capture(
            session,
            business.id,
            make_lead_payload(answers=BantAnswersIn(budget=STRONG.budget, need="just looking")),
        )
        before = lead.score

        await lead_service.update_config(
            session,
            business.id,
            QualificationWeightsUpdate(budget=100, authority=0, need=0, timeline=0),
        )
        rescored = await lead_service.rescore(session, business.id, lead.id)
        assert rescored.score > before

    async def test_changing_weights_does_not_move_existing_leads_on_its_own(
        self, session: AsyncSession, business: Business
    ):
        """A salesperson mid-call must not watch the pipeline reorder itself."""
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        before = lead.score

        await lead_service.update_config(
            session,
            business.id,
            QualificationWeightsUpdate(budget=0, authority=0, need=0, timeline=100),
        )
        assert (await lead_service.get(session, business.id, lead.id)).score == before

    async def test_the_config_round_trips_through_business_settings(
        self, session: AsyncSession, business: Business
    ):
        await lead_service.update_config(
            session, business.id, QualificationWeightsUpdate(hot_at=70, currency_floor=25_000)
        )
        config = await lead_service.get_config(session, business.id)
        assert config.hot_at == 70
        assert config.currency_floor == 25_000
        # Untouched keys keep their defaults rather than being wiped.
        assert config.weights == bant.DEFAULT_WEIGHTS


@pytest.mark.asyncio
class TestCaptureFromCall:
    async def test_a_call_produces_a_lead_linked_to_it(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        lead = await lead_service.capture_from_call(
            session,
            business.id,
            call_id=call.id,
            agent_id=agent.id,
            contact_name="Meera",
            contact_phone=call.caller_number,
            answers=STRONG,
        )
        assert lead.call_id == call.id
        assert lead.agent_id == agent.id
        assert lead.source == LeadSource.VOICE_CALL

    async def test_a_second_pass_sharpens_the_same_lead(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        """A caller who revises an answer must not become two prospects."""
        first = await lead_service.capture_from_call(
            session,
            business.id,
            call_id=call.id,
            agent_id=agent.id,
            contact_name="Meera",
            contact_phone=call.caller_number,
            answers=BantAnswers(need=STRONG.need),
        )
        second = await lead_service.capture_from_call(
            session,
            business.id,
            call_id=call.id,
            agent_id=agent.id,
            contact_name="Meera",
            contact_phone=call.caller_number,
            answers=BantAnswers(budget=STRONG.budget),
        )

        assert second.id == first.id
        # The second pass added budget without blanking the need it never asked.
        assert second.need_answer == STRONG.need
        assert second.budget_answer == STRONG.budget

    async def test_a_second_pass_rescores_rather_than_keeping_the_stale_number(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        first = await lead_service.capture_from_call(
            session,
            business.id,
            call_id=call.id,
            agent_id=agent.id,
            contact_name="Meera",
            contact_phone=call.caller_number,
            answers=BantAnswers(need="just looking"),
        )
        before = first.score
        second = await lead_service.capture_from_call(
            session,
            business.id,
            call_id=call.id,
            agent_id=agent.id,
            contact_name="Meera",
            contact_phone=call.caller_number,
            answers=STRONG,
        )
        assert second.score > before


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestUpdate:
    async def test_correcting_an_answer_rescores_the_lead(
        self, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(
            session, business.id, make_lead_payload(answers=BantAnswersIn(need="just looking"))
        )
        before = lead.score

        updated = await lead_service.update(
            session,
            business.id,
            lead.id,
            LeadUpdate(answers=BantAnswersIn(budget=STRONG.budget, need=STRONG.need)),
        )
        assert updated.score > before
        assert updated.rationale["need"] == "has an urgent, stated problem"

    async def test_a_scoring_outcome_cannot_be_set_by_hand(
        self, session: AsyncSession, business: Business
    ):
        """Marking a 12/100 lead "qualified" would make every score meaningless."""
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        with pytest.raises(ValidationError):
            await lead_service.update(
                session, business.id, lead.id, LeadUpdate(status=LeadStatus.QUALIFIED)
            )

    async def test_staff_own_the_status_once_they_pick_a_lead_up(
        self, session: AsyncSession, business: Business
    ):
        """Rescoring a converted deal must not drag it back to "qualified"."""
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.update(
            session, business.id, lead.id, LeadUpdate(status=LeadStatus.CONVERTED)
        )
        rescored = await lead_service.rescore(session, business.id, lead.id)
        assert rescored.status == LeadStatus.CONVERTED

    async def test_a_soft_deleted_lead_is_gone_from_the_pipeline(
        self, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.soft_delete(session, business.id, lead.id)

        leads, total = await lead_service.list_leads(session, business.id)
        assert total == 0 and leads == []
        with pytest.raises(NotFoundError):
            await lead_service.get(session, business.id, lead.id)


# --------------------------------------------------------------------------- #
# Listing and the pipeline summary
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestPipeline:
    async def test_the_pipeline_is_a_work_queue_not_a_log(
        self, session: AsyncSession, business: Business
    ):
        """Highest score first — the point of the list is who to call next."""
        await lead_service.capture(
            session,
            business.id,
            make_lead_payload(contact_name="Cold Caller", answers=BantAnswersIn(need="browsing")),
        )
        await lead_service.capture(session, business.id, make_lead_payload(contact_name="Hot One"))

        leads, total = await lead_service.list_leads(session, business.id)
        assert total == 2
        assert [lead.contact_name for lead in leads] == ["Hot One", "Cold Caller"]

    async def test_filters_narrow_the_pipeline(self, session: AsyncSession, business: Business):
        await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.capture(
            session,
            business.id,
            make_lead_payload(
                contact_name="Browser",
                contact_phone="9000011111",
                answers=BantAnswersIn(need="just looking"),
            ),
        )

        hot, _ = await lead_service.list_leads(session, business.id, tier=LeadTier.HOT)
        assert [lead.tier for lead in hot] == [LeadTier.HOT]

        scored, _ = await lead_service.list_leads(session, business.id, min_score=80)
        assert all(lead.score >= 80 for lead in scored)

        matched, _ = await lead_service.list_leads(session, business.id, contact_phone="9000011111")
        assert [lead.contact_name for lead in matched] == ["Browser"]

    async def test_the_summary_counts_tiers_statuses_and_averages(
        self, session: AsyncSession, business: Business
    ):
        await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.capture(
            session,
            business.id,
            make_lead_payload(contact_name="Browser", answers=BantAnswersIn(need="just looking")),
        )

        summary = await lead_service.pipeline_summary(session, business.id)
        assert summary["total"] == 2
        assert summary["by_tier"][LeadTier.HOT] == 1
        assert summary["by_status"][LeadStatus.QUALIFIED] == 1
        assert 0 < summary["average_score"] < 100
        assert summary["qualified_rate"] == 0.5

    async def test_an_empty_pipeline_summarises_without_dividing_by_zero(
        self, session: AsyncSession, business: Business
    ):
        summary = await lead_service.pipeline_summary(session, business.id)
        assert summary == {
            "total": 0,
            "by_tier": {tier.value: 0 for tier in LeadTier},
            "by_status": {status.value: 0 for status in LeadStatus},
            "average_score": 0.0,
            "qualified_rate": 0.0,
        }

    async def test_a_date_window_excludes_leads_outside_it(
        self, session: AsyncSession, business: Business
    ):
        await lead_service.capture(session, business.id, make_lead_payload())
        past = datetime.now(UTC) - timedelta(days=1)
        future = datetime.now(UTC) + timedelta(days=1)

        _, inside = await lead_service.list_leads(
            session, business.id, date_from=past, date_to=future
        )
        _, outside = await lead_service.list_leads(session, business.id, date_from=future)
        assert inside == 1
        assert outside == 0


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestLeadTenancy:
    async def test_a_lead_is_invisible_to_another_tenant(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        with pytest.raises(NotFoundError):
            await lead_service.get(session, other_business.id, lead.id)
        _, total = await lead_service.list_leads(session, other_business.id)
        assert total == 0

    async def test_another_tenant_cannot_edit_or_delete_a_lead(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        with pytest.raises(NotFoundError):
            await lead_service.update(
                session, other_business.id, lead.id, LeadUpdate(contact_name="Hijacked")
            )
        with pytest.raises(NotFoundError):
            await lead_service.soft_delete(session, other_business.id, lead.id)

    async def test_scoring_rules_are_per_tenant(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        await lead_service.update_config(
            session, business.id, QualificationWeightsUpdate(hot_at=55)
        )
        assert (await lead_service.get_config(session, other_business.id)).hot_at == (
            bant.DEFAULT_HOT_AT
        )


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestLeadsAPI:
    async def test_a_lead_can_be_created_and_read_back_with_its_breakdown(
        self, client: AsyncClient, owner_headers: dict
    ):
        response = await client.post(
            "/api/v1/leads",
            headers=owner_headers,
            json={
                "contact_name": "Rohit Sharma",
                "contact_phone": "9876500022",
                "company": "Sharma Textiles",
                "answers": {
                    "budget": STRONG.budget,
                    "authority": STRONG.authority,
                    "need": STRONG.need,
                    "timeline": STRONG.timeline,
                },
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["tier"] == LeadTier.HOT
        assert body["status"] == LeadStatus.QUALIFIED

        detail = await client.get(f"/api/v1/leads/{body['id']}", headers=owner_headers)
        assert detail.status_code == 200
        breakdown = detail.json()["breakdown"]
        assert [row["dimension"] for row in breakdown] == [d.value for d in BANTDimension]
        assert all(row["reason"] for row in breakdown)
        assert next(r for r in breakdown if r["dimension"] == "need")["percent"] == 100

    async def test_the_pipeline_lists_and_filters(
        self, client: AsyncClient, owner_headers: dict, session: AsyncSession, business: Business
    ):
        await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.capture(
            session,
            business.id,
            make_lead_payload(contact_name="Browser", answers=BantAnswersIn(need="just looking")),
        )

        listing = await client.get("/api/v1/leads", headers=owner_headers)
        assert listing.status_code == 200
        assert listing.json()["total"] == 2

        filtered = await client.get(
            "/api/v1/leads", headers=owner_headers, params={"tier": LeadTier.HOT.value}
        )
        assert [item["contact_name"] for item in filtered.json()["items"]] == ["Rohit Sharma"]

    async def test_the_summary_endpoint_backs_the_pipeline_header(
        self, client: AsyncClient, owner_headers: dict, session: AsyncSession, business: Business
    ):
        await lead_service.capture(session, business.id, make_lead_payload())

        response = await client.get("/api/v1/leads/summary", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["by_tier"]["hot"] == 1

    async def test_summary_is_not_parsed_as_a_lead_id(
        self, client: AsyncClient, owner_headers: dict
    ):
        """The static routes must win over ``/{lead_id}``."""
        for path in ("/api/v1/leads/summary", "/api/v1/leads/config"):
            assert (await client.get(path, headers=owner_headers)).status_code == 200

    async def test_correcting_an_answer_over_the_api_rescores(
        self, client: AsyncClient, owner_headers: dict, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(
            session, business.id, make_lead_payload(answers=BantAnswersIn(need="browsing"))
        )
        before = lead.score

        response = await client.patch(
            f"/api/v1/leads/{lead.id}",
            headers=owner_headers,
            json={"answers": {"budget": STRONG.budget, "need": STRONG.need}},
        )
        assert response.status_code == 200
        assert response.json()["score"] > before

    async def test_a_scoring_outcome_is_rejected_over_the_api(
        self, client: AsyncClient, owner_headers: dict, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        response = await client.patch(
            f"/api/v1/leads/{lead.id}",
            headers=owner_headers,
            json={"status": LeadStatus.QUALIFIED.value},
        )
        assert response.status_code == 422

    async def test_rescore_applies_the_current_weights(
        self, client: AsyncClient, owner_headers: dict, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(
            session,
            business.id,
            make_lead_payload(answers=BantAnswersIn(budget=STRONG.budget, need="browsing")),
        )
        before = lead.score

        await client.put(
            "/api/v1/leads/config",
            headers=owner_headers,
            json={"budget": 100, "authority": 0, "need": 0, "timeline": 0},
        )
        response = await client.post(f"/api/v1/leads/{lead.id}/rescore", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["score"] > before

    async def test_config_round_trips_over_the_api(self, client: AsyncClient, owner_headers: dict):
        response = await client.put(
            "/api/v1/leads/config",
            headers=owner_headers,
            json={"hot_at": 75, "warm_at": 45, "currency_floor": 25_000},
        )
        assert response.status_code == 200
        assert response.json() == {
            "weights": {d.value: bant.DEFAULT_WEIGHTS[d] for d in BANTDimension},
            "hot_at": 75,
            "warm_at": 45,
            "qualify_at": bant.DEFAULT_QUALIFY_AT,
            "currency_floor": 25_000,
        }

    async def test_a_viewer_can_read_but_not_write(
        self,
        client: AsyncClient,
        viewer_headers: dict,
        session: AsyncSession,
        business: Business,
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        assert (await client.get("/api/v1/leads", headers=viewer_headers)).status_code == 200
        assert (
            await client.patch(
                f"/api/v1/leads/{lead.id}", headers=viewer_headers, json={"company": "Nope"}
            )
        ).status_code == 403
        assert (
            await client.delete(f"/api/v1/leads/{lead.id}", headers=viewer_headers)
        ).status_code == 403

    async def test_only_an_admin_can_change_the_scoring_rules(
        self, client: AsyncClient, supervisor_headers: dict
    ):
        response = await client.put(
            "/api/v1/leads/config", headers=supervisor_headers, json={"hot_at": 10}
        )
        assert response.status_code == 403

    async def test_another_tenant_gets_a_404_not_a_leak(
        self,
        client: AsyncClient,
        other_headers: dict,
        session: AsyncSession,
        business: Business,
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        response = await client.get(f"/api/v1/leads/{lead.id}", headers=other_headers)
        assert response.status_code == 404

    async def test_the_endpoint_requires_authentication(self, client: AsyncClient):
        assert (await client.get("/api/v1/leads")).status_code == 401

    async def test_deleting_a_lead_removes_it_from_the_pipeline(
        self, client: AsyncClient, owner_headers: dict, session: AsyncSession, business: Business
    ):
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        assert (
            await client.delete(f"/api/v1/leads/{lead.id}", headers=owner_headers)
        ).status_code == 204
        assert (
            await client.get(f"/api/v1/leads/{lead.id}", headers=owner_headers)
        ).status_code == 404

    async def test_an_unknown_lead_is_a_404(self, client: AsyncClient, owner_headers: dict):
        response = await client.get(f"/api/v1/leads/{uuid.uuid4()}", headers=owner_headers)
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# CRM handoff
# --------------------------------------------------------------------------- #
class TestCrmConfig:
    def test_an_unconfigured_tenant_has_no_destination(self):
        assert bool(crm.load_crm_config({}).configured) is False

    def test_a_non_http_url_is_ignored(self):
        """A typo'd webhook must read as "unconfigured", not as a destination."""
        config = crm.load_crm_config({"crm": {"webhook_url": "crm.example.com/hook"}})
        assert config.configured is False

    def test_a_full_block_is_read(self):
        config = crm.load_crm_config(
            {
                "crm": {
                    "webhook_url": "https://crm.example.com/hook",
                    "secret": "s3cret",
                    "headers": {"X-Tenant": "sunrise"},
                    "min_score": 70,
                }
            }
        )
        assert config.configured
        assert config.headers == {"X-Tenant": "sunrise"}
        assert config.min_score == 70

    def test_a_malformed_block_does_not_raise(self):
        config = crm.load_crm_config({"crm": {"headers": "nope", "min_score": "high"}})
        assert config.headers == {}
        assert config.min_score is None


@pytest.mark.asyncio
class TestCrmPayload:
    async def test_the_payload_carries_the_answers_behind_the_score(
        self, session: AsyncSession, business: Business
    ):
        """A sales manager must be able to question the number in their own CRM."""
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        payload = crm.lead_payload(lead)

        assert payload["event"] == "lead.qualified"
        assert payload["contact"]["phone"] == "+919876500022"
        assert payload["bant"]["need"]["answer"] == STRONG.need
        assert payload["bant"]["need"]["reason"]
        assert payload["score"] == lead.score


@pytest.mark.asyncio
class TestPushQualifiedLeads:
    async def _configure(self, business: Business, **overrides) -> None:
        business.settings_json = {
            **(business.settings_json or {}),
            "crm": {"webhook_url": "https://crm.example.com/hook", **overrides},
        }

    async def test_a_qualified_lead_reaches_the_crm(
        self,
        session: AsyncSession,
        business: Business,
        fake_crm: FakeCrmClient,
    ):
        await self._configure(business)
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        result = await jobs.push_qualified_leads(session)

        assert result.detail == {"sent": 1, "failed": 0, "skipped": 0}
        assert [lead_id for _, lead_id in fake_crm.pushed] == [str(lead.id)]
        assert lead.crm_status == CrmPushStatus.SENT
        assert lead.crm_reference == "crm-1"
        assert lead.crm_pushed_at is not None

    async def test_an_unqualified_lead_is_never_pushed(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        await self._configure(business)
        await lead_service.capture(
            session, business.id, make_lead_payload(answers=BantAnswersIn(need="just looking"))
        )

        result = await jobs.push_qualified_leads(session)
        assert result.detail["sent"] == 0
        assert fake_crm.pushed == []

    async def test_a_tenant_without_a_webhook_parks_the_lead(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        """No destination is a settled state, not a failure to retry forever."""
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        result = await jobs.push_qualified_leads(session)
        assert result.detail["skipped"] == 1
        assert lead.crm_status == CrmPushStatus.NOT_CONFIGURED
        assert fake_crm.pushed == []

        # A second pass finds nothing left to do.
        assert (await jobs.push_qualified_leads(session)).detail["skipped"] == 0

    async def test_a_lead_below_the_tenants_floor_is_held_back(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        await self._configure(business, min_score=95)
        # Qualified, but not a perfect 100 — the tenant only wants the very top
        # of their pipeline reaching the CRM.
        lead = await lead_service.capture(
            session,
            business.id,
            make_lead_payload(
                answers=BantAnswersIn(
                    budget=STRONG.budget,
                    authority=STRONG.authority,
                    need="we are evaluating a new booking system",
                    timeline=STRONG.timeline,
                )
            ),
        )
        assert lead.status == LeadStatus.QUALIFIED and lead.score < 95

        result = await jobs.push_qualified_leads(session)
        assert result.detail["skipped"] == 1
        assert fake_crm.pushed == []
        assert "floor" in (lead.crm_error or "")

    async def test_a_refused_push_is_retried_on_the_next_pass(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        await self._configure(business)
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        fake_crm.fail = True
        assert (await jobs.push_qualified_leads(session)).detail["failed"] == 1
        assert lead.crm_status == CrmPushStatus.FAILED
        assert lead.crm_attempts == 1
        assert lead.crm_error == "injected CRM rejection"

        fake_crm.fail = False
        assert (await jobs.push_qualified_leads(session)).detail["sent"] == 1
        assert lead.crm_status == CrmPushStatus.SENT
        assert lead.crm_error is None

    async def test_an_unreachable_crm_does_not_strand_the_batch(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        """One tenant's endpoint blowing up must not lose everyone else's leads."""
        await self._configure(business)
        lead = await lead_service.capture(session, business.id, make_lead_payload())

        fake_crm.raise_error = True
        result = await jobs.push_qualified_leads(session)

        assert result.detail["failed"] == 1
        assert lead.crm_status == CrmPushStatus.FAILED
        assert "RuntimeError" in (lead.crm_error or "")

    async def test_a_lead_is_abandoned_after_too_many_attempts(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        """A CRM that has refused ten times is misconfigured, not slow."""
        await self._configure(business)
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        lead.crm_attempts = 10
        lead.crm_status = CrmPushStatus.FAILED
        await session.flush()

        assert (await jobs.push_qualified_leads(session)).detail == {
            "sent": 0,
            "failed": 0,
            "skipped": 0,
        }
        assert fake_crm.pushed == []

    async def test_a_delivered_lead_is_not_pushed_twice(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        await self._configure(business)
        await lead_service.capture(session, business.id, make_lead_payload())

        assert (await jobs.push_qualified_leads(session)).detail["sent"] == 1
        assert (await jobs.push_qualified_leads(session)).detail["sent"] == 0
        assert len(fake_crm.pushed) == 1

    async def test_each_tenant_gets_its_own_destination(
        self,
        session: AsyncSession,
        business: Business,
        other_business: Business,
        fake_crm: FakeCrmClient,
    ):
        await self._configure(business)
        other_business.settings_json = {"crm": {"webhook_url": "https://rival.example.com/hook"}}
        await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.capture(session, other_business.id, make_lead_payload())

        await jobs.push_qualified_leads(session)
        assert sorted(config.webhook_url or "" for config, _ in fake_crm.pushed) == [
            "https://crm.example.com/hook",
            "https://rival.example.com/hook",
        ]

    async def test_an_empty_queue_is_a_cheap_no_op(self, session: AsyncSession):
        assert (await jobs.push_qualified_leads(session)).detail == {
            "sent": 0,
            "failed": 0,
            "skipped": 0,
        }

    async def test_a_soft_deleted_lead_is_not_pushed(
        self, session: AsyncSession, business: Business, fake_crm: FakeCrmClient
    ):
        await self._configure(business)
        lead = await lead_service.capture(session, business.id, make_lead_payload())
        await lead_service.soft_delete(session, business.id, lead.id)

        assert (await jobs.push_qualified_leads(session)).detail["sent"] == 0
        assert fake_crm.pushed == []


# --------------------------------------------------------------------------- #
# The qualify_lead flow node
# --------------------------------------------------------------------------- #
def qualification_flow(**node_overrides) -> dict:
    """A flow that goes straight to the qualify node on the first utterance."""
    node = {
        "id": "qualify",
        "type": "qualify_lead",
        "next": "wrap_up",
        "on_hot": "sales_priority",
        "on_unqualified": "polite_close",
        "unqualified_message": "Thank you for your time.",
        **node_overrides,
    }
    return {
        "start_node": "ask_need",
        "nodes": [
            {
                "id": "ask_need",
                "type": "collect",
                "prompt": "What are you looking for?",
                "variable": "need",
                "next": "qualify",
            },
            node,
            {
                "id": "wrap_up",
                "type": "end",
                "text": "Someone will call you.",
                "disposition": "resolved",
            },
            {
                "id": "polite_close",
                "type": "end",
                "text": "Thanks for calling.",
                "disposition": "resolved",
            },
            {
                "id": "sales_priority",
                "type": "message",
                "text": "Putting you through now.",
                "next": "wrap_up",
            },
        ],
    }


@pytest.mark.asyncio
class TestQualifyLeadNode:
    async def _run(
        self,
        session: AsyncSession,
        call: CallLog,
        agent: VoiceAgent,
        utterance: str,
        *,
        variables: dict | None = None,
        flow: dict | None = None,
    ):
        from app.services.conversation_engine import get_engine

        agent.flow_json = flow or qualification_flow()
        call.metadata_json = {
            "state": {
                "turn_index": 1,
                "current_node": "qualify",
                "variables": variables or {},
            }
        }
        await session.flush()
        return await get_engine().process_turn(session, call, agent, utterance)

    async def test_the_node_writes_a_scored_lead_into_the_pipeline(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        await self._run(
            session,
            call,
            agent,
            "yes",
            variables={
                "contact_name": "Priya Nair",
                "budget": STRONG.budget,
                "authority": STRONG.authority,
                "need": STRONG.need,
                "timeline": STRONG.timeline,
            },
        )

        leads, total = await lead_service.list_leads(session, business.id)
        assert total == 1
        lead = leads[0]
        assert lead.contact_name == "Priya Nair"
        assert lead.contact_phone == call.caller_number
        assert lead.call_id == call.id
        assert lead.tier == LeadTier.HOT
        assert lead.source == LeadSource.VOICE_CALL

    async def test_a_hot_lead_takes_the_priority_route(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ):
        result = await self._run(
            session,
            call,
            agent,
            "yes",
            flow=qualification_flow(on_hot="sales_priority"),
            variables={
                "contact_name": "Priya",
                "budget": STRONG.budget,
                "authority": STRONG.authority,
                "need": STRONG.need,
                "timeline": STRONG.timeline,
            },
        )
        assert "Putting you through now." in result.reply

    async def test_an_unqualified_caller_takes_the_polite_close(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ):
        result = await self._run(
            session,
            call,
            agent,
            "no",
            variables={
                "contact_name": "Browser",
                "budget": WEAK.budget,
                "authority": WEAK.authority,
                "need": WEAK.need,
                "timeline": WEAK.timeline,
            },
        )
        assert "Thank you for your time." in result.reply
        assert "Thanks for calling." in result.reply
        assert result.should_end_call is True

    async def test_the_score_is_exposed_to_later_nodes(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        """A flow author routes on these, and the LLM sees them as context."""
        result = await self._run(
            session,
            call,
            agent,
            "yes",
            variables={
                "contact_name": "Priya",
                "budget": STRONG.budget,
                "authority": STRONG.authority,
                "need": STRONG.need,
                "timeline": STRONG.timeline,
            },
        )
        assert result.variables["lead_tier"] == LeadTier.HOT
        assert result.variables["lead_score"] >= 80
        assert uuid.UUID(result.variables["lead_id"])

    async def test_a_caller_who_answered_nothing_still_completes_the_call(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        """Scoring must not be able to fail on caller input."""
        result = await self._run(session, call, agent, "hmm", variables={})

        assert result.reply
        leads, total = await lead_service.list_leads(session, business.id)
        assert total == 1
        assert leads[0].tier == LeadTier.UNQUALIFIED
        assert leads[0].contact_name == "Phone caller"

    async def test_the_starter_flow_qualifies_end_to_end(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ):
        """The flow a lead-qualification agent is created with must actually work."""
        from app.services.conversation_engine import get_engine
        from app.services.flow import lead_qualification_flow

        agent.flow_json = lead_qualification_flow("Namaste!")
        call.metadata_json = {"state": {"turn_index": 0}}
        await session.flush()

        engine = get_engine()
        for utterance in (
            "Priya Nair",
            STRONG.need,
            STRONG.timeline,
            STRONG.budget,
            STRONG.authority,
        ):
            await engine.process_turn(session, call, agent, utterance)

        leads, total = await lead_service.list_leads(session, business.id)
        assert total == 1
        assert leads[0].contact_name == "Priya Nair"
        assert leads[0].tier == LeadTier.HOT


class TestLeadModel:
    def test_a_purged_call_does_not_take_the_lead_with_it(self):
        """Retention purges calls; a salesperson's pipeline must survive that.

        Asserted against the schema rather than by deleting a call, because it
        is the database that enforces this — the ORM would happily do something
        else if the constraint were wrong.
        """
        ondelete = {
            next(iter(fk.columns)).name: fk.ondelete
            for fk in Lead.__table__.foreign_key_constraints
        }
        assert ondelete["call_id"] == "SET NULL"
        assert ondelete["agent_id"] == "SET NULL"
        # The tenant going away is the one case where the lead should too.
        assert ondelete["business_id"] == "CASCADE"
