"""Call quality monitoring: grading, aggregation, events and the API."""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError
from app.core.events import EventType
from app.models.enums import QualityGrade
from app.services import call_quality_service


class TestGradeSample:
    def test_clean_call_grades_good(self):
        grade = call_quality_service.grade_sample(latency_ms=80, jitter_ms=5.0, packet_loss_pct=0.1)
        assert grade == QualityGrade.GOOD

    def test_moderate_latency_grades_fair(self):
        grade = call_quality_service.grade_sample(
            latency_ms=250, jitter_ms=5.0, packet_loss_pct=0.1
        )
        assert grade == QualityGrade.FAIR

    def test_high_packet_loss_alone_grades_poor(self):
        grade = call_quality_service.grade_sample(latency_ms=50, jitter_ms=5.0, packet_loss_pct=8.0)
        assert grade == QualityGrade.POOR

    def test_the_worst_dimension_decides_the_grade(self):
        """Good latency and jitter cannot rescue a call with terrible packet loss."""
        grade = call_quality_service.grade_sample(
            latency_ms=10, jitter_ms=1.0, packet_loss_pct=99.0
        )
        assert grade == QualityGrade.POOR

    def test_boundary_values_are_inclusive(self):
        assert call_quality_service.grade_sample(
            latency_ms=call_quality_service.FAIR_LATENCY_MS, jitter_ms=0, packet_loss_pct=0
        ) == QualityGrade.FAIR
        assert call_quality_service.grade_sample(
            latency_ms=call_quality_service.POOR_LATENCY_MS, jitter_ms=0, packet_loss_pct=0
        ) == QualityGrade.POOR


class TestRecordSample:
    async def test_stores_a_graded_sample(self, session, business, call):
        sample = await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=90, jitter_ms=8.0, packet_loss_pct=0.2
        )
        assert sample.grade == QualityGrade.GOOD
        assert sample.call_id == call.id

    async def test_unknown_call_is_a_404(self, session, business):
        import uuid

        with pytest.raises(NotFoundError):
            await call_quality_service.record_sample(
                session,
                business.id,
                uuid.uuid4(),
                latency_ms=90,
                jitter_ms=8.0,
                packet_loss_pct=0.2,
            )

    async def test_another_tenant_cannot_record_against_the_call(
        self, session, other_business, call
    ):
        with pytest.raises(NotFoundError):
            await call_quality_service.record_sample(
                session,
                other_business.id,
                call.id,
                latency_ms=90,
                jitter_ms=8.0,
                packet_loss_pct=0.2,
            )

    async def test_good_sample_emits_quality_sample_event(self, session, business, call, event_bus):
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=50, jitter_ms=2.0, packet_loss_pct=0.0
        )
        types = [e.type for e in event_bus.published]
        assert EventType.QUALITY_SAMPLE in types
        assert EventType.QUALITY_DEGRADED not in types

    async def test_poor_sample_emits_quality_degraded_event(
        self, session, business, call, event_bus
    ):
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=800, jitter_ms=2.0, packet_loss_pct=0.0
        )
        types = [e.type for e in event_bus.published]
        assert EventType.QUALITY_DEGRADED in types

        event = next(e for e in event_bus.published if e.type == EventType.QUALITY_DEGRADED)
        assert event.call_id == call.id
        assert event.data["grade"] == QualityGrade.POOR.value


class TestSummary:
    async def test_no_samples_yields_an_empty_summary(self, session, business, call):
        summary = await call_quality_service.get_summary(session, business.id, call.id)
        assert summary.sample_count == 0
        assert summary.avg_latency_ms is None
        assert summary.worst_grade is None

    async def test_aggregates_across_samples(self, session, business, call):
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=100, jitter_ms=10.0, packet_loss_pct=0.0
        )
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=300, jitter_ms=20.0, packet_loss_pct=2.0
        )

        summary = await call_quality_service.get_summary(session, business.id, call.id)
        assert summary.sample_count == 2
        assert summary.avg_latency_ms == pytest.approx(200.0)
        assert summary.max_latency_ms == 300
        assert summary.worst_grade == QualityGrade.FAIR

    async def test_worst_grade_reflects_the_single_worst_sample(self, session, business, call):
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=10, jitter_ms=1.0, packet_loss_pct=0.0
        )
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=10, jitter_ms=1.0, packet_loss_pct=90.0
        )
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=10, jitter_ms=1.0, packet_loss_pct=0.0
        )

        summary = await call_quality_service.get_summary(session, business.id, call.id)
        assert summary.worst_grade == QualityGrade.POOR
        assert summary.sample_count == 3


class TestListSamples:
    async def test_returns_samples_oldest_first(self, session, business, call):
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=100, jitter_ms=1.0, packet_loss_pct=0.0
        )
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=110, jitter_ms=1.0, packet_loss_pct=0.0
        )

        samples = await call_quality_service.list_samples(session, business.id, call.id)
        assert [s.latency_ms for s in samples] == [100, 110]

    async def test_tenant_scoped(self, session, business, other_business, call):
        await call_quality_service.record_sample(
            session, business.id, call.id, latency_ms=100, jitter_ms=1.0, packet_loss_pct=0.0
        )
        with pytest.raises(NotFoundError):
            await call_quality_service.list_samples(session, other_business.id, call.id)


class TestQualityEndpoints:
    async def test_ingest_and_list(self, client, owner_headers, call):
        response = await client.post(
            f"/api/v1/calls/{call.id}/quality",
            headers=owner_headers,
            json={"latency_ms": 120, "jitter_ms": 15.0, "packet_loss_pct": 0.5},
        )
        assert response.status_code == 201
        assert response.json()["grade"] == "good"

        listing = await client.get(f"/api/v1/calls/{call.id}/quality", headers=owner_headers)
        assert len(listing.json()) == 1

    async def test_summary_endpoint(self, client, owner_headers, call):
        await client.post(
            f"/api/v1/calls/{call.id}/quality",
            headers=owner_headers,
            json={"latency_ms": 500, "jitter_ms": 15.0, "packet_loss_pct": 0.5},
        )

        response = await client.get(
            f"/api/v1/calls/{call.id}/quality/summary", headers=owner_headers
        )
        body = response.json()
        assert body["sample_count"] == 1
        assert body["worst_grade"] == "poor"

    async def test_viewer_cannot_ingest_samples(self, client, viewer_headers, call):
        response = await client.post(
            f"/api/v1/calls/{call.id}/quality",
            headers=viewer_headers,
            json={"latency_ms": 120, "jitter_ms": 15.0, "packet_loss_pct": 0.5},
        )
        assert response.status_code == 403

    async def test_out_of_range_values_are_rejected(self, client, owner_headers, call):
        response = await client.post(
            f"/api/v1/calls/{call.id}/quality",
            headers=owner_headers,
            json={"latency_ms": 120, "jitter_ms": 15.0, "packet_loss_pct": 150.0},
        )
        assert response.status_code == 422

    async def test_another_tenant_cannot_read_samples(self, client, other_headers, call):
        response = await client.get(f"/api/v1/calls/{call.id}/quality", headers=other_headers)
        assert response.status_code == 404
