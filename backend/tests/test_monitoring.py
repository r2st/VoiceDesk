"""Live call monitoring: the event bus, the live board, takeover and the socket.

Design doc §4.3. The WebSocket endpoint is exercised by calling the route
coroutine with a stand-in socket rather than through an HTTP client: httpx has
no WebSocket transport, and the parts worth testing — token auth, tenancy, the
opening snapshot and per-call filtering — are all in the handler.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import WebSocketDisconnect
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import monitor as monitor_api
from app.core import events as events_module
from app.core.deps import TenantContext
from app.core.errors import ConflictError, NotFoundError, PermissionError_
from app.core.events import EventType, InMemoryEventBus, LiveEvent, emit
from app.core.security import create_access_token
from app.core.timeutil import ensure_utc
from app.models.business import Business, User
from app.models.call import CallLog, CallTakeover, Conversation
from app.models.enums import CallStatus, SpeakerRole, UserRole
from app.models.voice_agent import VoiceAgent
from app.services import monitoring
from app.services.conversation_engine import get_engine
from app.services.telephony import WebhookEvent


def context_for(user: User) -> TenantContext:
    return TenantContext(
        user_id=user.id,
        business_id=user.business_id,
        role=UserRole(user.role),
        email=user.email,
    )


def token_for(user: User) -> str:
    token, _ = create_access_token(
        user_id=user.id, business_id=user.business_id, role=user.role
    )
    return token


class FakeWebSocket:
    """Records what the handler sends. Enough surface for the monitor route."""

    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self.closed: tuple[int, str | None] | None = None
        #: Set once a frame arrives, so a test can await the relay deterministically.
        self.received = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        self.received.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    def frames_of_type(self, event_type: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == event_type]


async def run_stream(socket: FakeWebSocket, token: str, call_id: uuid.UUID | None = None):
    """Start the WebSocket handler in the background; the caller cancels it."""
    task = asyncio.create_task(monitor_api.stream(socket, token, call_id))
    await asyncio.sleep(0)  # let it reach the first await
    return task


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# The event bus
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestEventBus:
    async def test_a_published_event_reaches_a_subscriber(
        self, event_bus: InMemoryEventBus
    ) -> None:
        business_id = uuid.uuid4()
        stream = event_bus.subscribe(business_id)
        received: list[LiveEvent] = []

        async def collect() -> None:
            async for event in stream:
                received.append(event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await emit(EventType.CALL_STATUS, business_id, status="ringing")
        await asyncio.sleep(0)
        await stop(task)

        assert [e.type for e in received] == [EventType.CALL_STATUS]
        assert received[0].data["status"] == "ringing"

    async def test_one_tenants_events_never_reach_another(
        self, event_bus: InMemoryEventBus
    ) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        stream = event_bus.subscribe(mine)
        received: list[LiveEvent] = []

        async def collect() -> None:
            async for event in stream:
                received.append(event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await emit(EventType.CALL_STATUS, theirs, status="ringing")
        await asyncio.sleep(0)
        await stop(task)

        assert received == []

    async def test_an_event_survives_a_json_round_trip(self) -> None:
        original = LiveEvent(
            type=EventType.TRANSCRIPT_TURN,
            business_id=uuid.uuid4(),
            call_id=uuid.uuid4(),
            data={"content": "नमस्ते", "confidence": 0.91},
        )
        restored = LiveEvent.from_json(original.to_json())

        assert restored.type == original.type
        assert restored.business_id == original.business_id
        assert restored.call_id == original.call_id
        assert restored.data == original.data

    async def test_a_subscriber_that_stops_reading_drops_the_oldest_events(self) -> None:
        """A dashboard left open on a locked laptop must not grow without bound."""
        bus = InMemoryEventBus(max_queue=2)
        business_id = uuid.uuid4()
        stream = bus.subscribe(business_id)

        async def publish(marker: int) -> None:
            await bus.publish(
                LiveEvent(type=EventType.CALL_STATUS, business_id=business_id, data={"i": marker})
            )

        # The generator registers its queue on first iteration, so prime it.
        pending = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        await publish(0)
        assert (await pending).data["i"] == 0

        # Three events into a two-slot queue: the oldest falls off the front.
        for marker in (1, 2, 3):
            await publish(marker)
        received = [(await stream.__anext__()).data["i"] for _ in range(2)]
        await stream.aclose()

        assert received == [2, 3]


# --------------------------------------------------------------------------- #
# The live board
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestLiveBoard:
    async def test_in_progress_calls_appear_with_their_last_turn(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        session.add_all(
            [
                Conversation(
                    business_id=business.id,
                    call_id=call.id,
                    turn_index=0,
                    role=SpeakerRole.AGENT,
                    content="Namaste!",
                ),
                Conversation(
                    business_id=business.id,
                    call_id=call.id,
                    turn_index=1,
                    role=SpeakerRole.CALLER,
                    content="Mujhe appointment chahiye",
                ),
            ]
        )
        await session.flush()

        board = await monitoring.list_live_calls(session, business.id)

        assert len(board) == 1
        assert board[0].turn_count == 2
        assert board[0].last_turn is not None
        assert board[0].last_turn.content == "Mujhe appointment chahiye"
        assert board[0].is_supervised is False

    async def test_a_completed_call_leaves_the_board(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        call.status = CallStatus.COMPLETED
        await session.flush()

        assert await monitoring.list_live_calls(session, business.id) == []

    async def test_another_tenants_live_call_is_invisible(
        self, session: AsyncSession, other_business: Business, call: CallLog
    ) -> None:
        assert await monitoring.list_live_calls(session, other_business.id) == []

    async def test_the_endpoint_reports_elapsed_time_and_turn_counts(
        self,
        client: AsyncClient,
        session: AsyncSession,
        business: Business,
        call: CallLog,
        supervisor_headers: dict[str, str],
    ) -> None:
        session.add(
            Conversation(
                business_id=business.id,
                call_id=call.id,
                turn_index=0,
                role=SpeakerRole.AGENT,
                content="Namaste!",
            )
        )
        await session.flush()

        response = await client.get("/api/v1/monitor/live", headers=supervisor_headers)

        assert response.status_code == 200
        [row] = response.json()
        assert row["call_id"] == str(call.id)
        assert row["turn_count"] == 1
        assert row["last_speaker"] == SpeakerRole.AGENT
        assert row["elapsed_sec"] >= 0
        assert row["takeover"] is None

    async def test_the_snapshot_returns_the_transcript_so_far(
        self,
        client: AsyncClient,
        session: AsyncSession,
        business: Business,
        call: CallLog,
        supervisor_headers: dict[str, str],
    ) -> None:
        session.add(
            Conversation(
                business_id=business.id,
                call_id=call.id,
                turn_index=0,
                role=SpeakerRole.CALLER,
                content="Hello?",
            )
        )
        await session.flush()

        response = await client.get(
            f"/api/v1/monitor/calls/{call.id}", headers=supervisor_headers
        )

        assert response.status_code == 200
        body = response.json()
        assert body["call"]["id"] == str(call.id)
        assert [t["content"] for t in body["turns"]] == ["Hello?"]

    async def test_a_snapshot_of_another_tenants_call_is_a_404(
        self, client: AsyncClient, call: CallLog, other_headers: dict[str, str]
    ) -> None:
        response = await client.get(f"/api/v1/monitor/calls/{call.id}", headers=other_headers)

        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Takeover
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestTakeover:
    async def test_a_supervisor_takes_a_live_call(
        self,
        session: AsyncSession,
        supervisor: User,
        call: CallLog,
        event_bus: InMemoryEventBus,
    ) -> None:
        takeover = await monitoring.start_takeover(
            session, context_for(supervisor), call.id, reason="caller upset"
        )

        assert takeover.is_active
        assert takeover.supervisor_user_id == supervisor.id
        assert takeover.reason == "caller upset"
        assert [e.type for e in event_bus.published] == [EventType.TAKEOVER_STARTED]

    async def test_taking_over_twice_is_idempotent_for_the_same_supervisor(
        self, session: AsyncSession, supervisor: User, call: CallLog
    ) -> None:
        first = await monitoring.start_takeover(session, context_for(supervisor), call.id)
        second = await monitoring.start_takeover(session, context_for(supervisor), call.id)

        assert first.id == second.id

    async def test_a_second_supervisor_cannot_take_a_held_call(
        self,
        session: AsyncSession,
        supervisor: User,
        second_supervisor: User,
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)

        with pytest.raises(ConflictError, match="already handling"):
            await monitoring.start_takeover(session, context_for(second_supervisor), call.id)

    async def test_a_finished_call_cannot_be_taken_over(
        self, session: AsyncSession, supervisor: User, call: CallLog
    ) -> None:
        call.status = CallStatus.COMPLETED
        await session.flush()

        with pytest.raises(ConflictError, match="no longer be taken over"):
            await monitoring.start_takeover(session, context_for(supervisor), call.id)

    async def test_another_tenant_cannot_take_over_a_call_it_cannot_see(
        self, session: AsyncSession, other_owner: User, call: CallLog
    ) -> None:
        with pytest.raises(NotFoundError):
            await monitoring.start_takeover(session, context_for(other_owner), call.id)

    async def test_releasing_hands_the_call_back_to_the_ai(
        self,
        session: AsyncSession,
        supervisor: User,
        call: CallLog,
        event_bus: InMemoryEventBus,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        released = await monitoring.end_takeover(session, context_for(supervisor), call.id)

        assert released.ended_at is not None
        assert released.returned_to_ai is True
        assert await monitoring.active_takeover(session, call.business_id, call.id) is None
        assert event_bus.published[-1].type == EventType.TAKEOVER_ENDED

    async def test_a_colleague_cannot_release_someone_elses_takeover(
        self,
        session: AsyncSession,
        supervisor: User,
        second_supervisor: User,
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)

        with pytest.raises(PermissionError_):
            await monitoring.end_takeover(session, context_for(second_supervisor), call.id)

    async def test_an_owner_can_release_a_takeover_left_open(
        self, session: AsyncSession, supervisor: User, owner: User, call: CallLog
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        released = await monitoring.end_takeover(session, context_for(owner), call.id)

        assert released.ended_at is not None

    async def test_releasing_a_call_nobody_holds_is_a_404(
        self, session: AsyncSession, supervisor: User, call: CallLog
    ) -> None:
        with pytest.raises(NotFoundError):
            await monitoring.end_takeover(session, context_for(supervisor), call.id)

    async def test_the_takeover_endpoint_hands_the_call_to_the_supervisor(
        self,
        client: AsyncClient,
        call: CallLog,
        supervisor_headers: dict[str, str],
    ) -> None:
        response = await client.post(
            f"/api/v1/monitor/calls/{call.id}/takeover",
            headers=supervisor_headers,
            json={"reason": "caller confused"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["reason"] == "caller confused"
        assert body["ended_at"] is None

    async def test_the_release_endpoint_returns_the_call_to_the_ai(
        self,
        client: AsyncClient,
        session: AsyncSession,
        supervisor: User,
        call: CallLog,
        supervisor_headers: dict[str, str],
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)

        response = await client.post(
            f"/api/v1/monitor/calls/{call.id}/release",
            headers=supervisor_headers,
            json={},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ended_at"] is not None
        assert body["returned_to_ai"] is True

    async def test_a_takeover_is_closed_when_the_call_ends(
        self, session: AsyncSession, supervisor: User, call: CallLog
    ) -> None:
        from app.services import call_service

        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        await call_service.apply_webhook_event(
            session,
            WebhookEvent(
                provider_call_id=call.provider_call_id,
                status=CallStatus.COMPLETED,
                duration_sec=42,
            ),
            call.provider,
        )

        assert await monitoring.active_takeover(session, call.business_id, call.id) is None
        row = (
            await session.execute(select(CallTakeover).where(CallTakeover.call_id == call.id))
        ).scalar_one()
        assert row.returned_to_ai is False

    async def test_hanging_up_closes_the_takeover(
        self, session: AsyncSession, supervisor: User, business: Business, call: CallLog
    ) -> None:
        from app.services import call_service

        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        await call_service.hangup_call(session, business.id, call.id, reason="supervisor ended")

        assert await monitoring.active_takeover(session, business.id, call.id) is None


# --------------------------------------------------------------------------- #
# Speaking as the supervisor
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestSupervisorSpeech:
    async def test_speaking_files_a_human_turn_and_publishes_it(
        self,
        session: AsyncSession,
        supervisor: User,
        call: CallLog,
        event_bus: InMemoryEventBus,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        turn = await monitoring.speak(
            session, context_for(supervisor), call.id, "This is Priya, I can help."
        )

        assert turn.role == SpeakerRole.HUMAN
        assert turn.content == "This is Priya, I can help."
        assert turn.metadata_json["supervisor_user_id"] == str(supervisor.id)
        assert event_bus.published[-1].type == EventType.TRANSCRIPT_TURN
        assert event_bus.published[-1].data["role"] == SpeakerRole.HUMAN

    async def test_speaking_without_taking_over_is_refused(
        self, session: AsyncSession, supervisor: User, call: CallLog
    ) -> None:
        with pytest.raises(ConflictError, match="Take the call over"):
            await monitoring.speak(session, context_for(supervisor), call.id, "Hello?")

    async def test_a_colleague_cannot_speak_on_a_call_someone_else_holds(
        self,
        session: AsyncSession,
        supervisor: User,
        second_supervisor: User,
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)

        with pytest.raises(PermissionError_):
            await monitoring.speak(session, context_for(second_supervisor), call.id, "Hi")

    async def test_speech_continues_the_transcript_rather_than_colliding_with_it(
        self,
        session: AsyncSession,
        supervisor: User,
        call: CallLog,
        agent: VoiceAgent,
    ) -> None:
        """The human's turn must take the next index, not reuse the AI's."""
        await get_engine().start_call(session, call, agent)
        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        turn = await monitoring.speak(session, context_for(supervisor), call.id, "Taking over.")

        assert turn.turn_index == 1
        turns = (
            (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.call_id == call.id)
                    .order_by(Conversation.turn_index)
                )
            )
            .scalars()
            .all()
        )
        assert [t.role for t in turns] == [SpeakerRole.AGENT, SpeakerRole.HUMAN]

    async def test_the_say_endpoint_records_the_utterance(
        self,
        client: AsyncClient,
        session: AsyncSession,
        supervisor: User,
        supervisor_headers: dict[str, str],
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)

        response = await client.post(
            f"/api/v1/monitor/calls/{call.id}/say",
            headers=supervisor_headers,
            json={"text": "Let me check that for you."},
        )

        assert response.status_code == 201
        assert response.json()["role"] == SpeakerRole.HUMAN

    async def test_a_viewer_cannot_take_a_call_over(
        self, client: AsyncClient, call: CallLog, viewer_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            f"/api/v1/monitor/calls/{call.id}/takeover", headers=viewer_headers, json={}
        )

        assert response.status_code == 403


# --------------------------------------------------------------------------- #
# The engine yields to the human
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestEngineYieldsToHuman:
    async def test_the_ai_stays_silent_while_a_supervisor_holds_the_call(
        self,
        client: AsyncClient,
        session: AsyncSession,
        supervisor: User,
        supervisor_headers: dict[str, str],
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)

        response = await client.post(
            f"/api/v1/calls/{call.id}/turn",
            headers=supervisor_headers,
            json={"utterance": "Mujhe kal ka appointment chahiye"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["awaiting_human"] is True
        assert body["reply"] == ""

    async def test_the_callers_words_are_still_transcribed_during_a_takeover(
        self,
        client: AsyncClient,
        session: AsyncSession,
        supervisor: User,
        supervisor_headers: dict[str, str],
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        await client.post(
            f"/api/v1/calls/{call.id}/turn",
            headers=supervisor_headers,
            json={"utterance": "Mera naam Rahul hai"},
        )

        turns = (
            (
                await session.execute(
                    select(Conversation).where(Conversation.call_id == call.id)
                )
            )
            .scalars()
            .all()
        )
        assert [t.role for t in turns] == [SpeakerRole.CALLER]
        assert turns[0].content == "Mera naam Rahul hai"

    async def test_the_ai_resumes_once_the_call_is_released(
        self,
        client: AsyncClient,
        session: AsyncSession,
        supervisor: User,
        supervisor_headers: dict[str, str],
        call: CallLog,
    ) -> None:
        await monitoring.start_takeover(session, context_for(supervisor), call.id)
        await monitoring.end_takeover(session, context_for(supervisor), call.id)

        response = await client.post(
            f"/api/v1/calls/{call.id}/turn",
            headers=supervisor_headers,
            json={"utterance": "Namaste"},
        )

        body = response.json()
        assert body["awaiting_human"] is False
        assert body["reply"] != ""

    async def test_engine_turns_are_published_to_watchers(
        self,
        session: AsyncSession,
        call: CallLog,
        agent: VoiceAgent,
        event_bus: InMemoryEventBus,
    ) -> None:
        await get_engine().process_turn(session, call, agent, "Namaste")

        turn_events = [
            e for e in event_bus.published if e.type == EventType.TRANSCRIPT_TURN
        ]
        assert [e.data["role"] for e in turn_events] == [
            SpeakerRole.CALLER,
            SpeakerRole.AGENT,
        ]
        assert all(e.call_id == call.id for e in turn_events)


# --------------------------------------------------------------------------- #
# The WebSocket
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestMonitorSocket:
    async def test_an_invalid_token_is_rejected_before_the_handshake(self) -> None:
        socket = FakeWebSocket()

        await monitor_api.stream(socket, "not-a-token", None)

        assert socket.accepted is False
        assert socket.closed is not None
        assert socket.closed[0] == 1008

    async def test_a_deactivated_user_cannot_open_a_stream(
        self, session: AsyncSession, supervisor: User
    ) -> None:
        token = token_for(supervisor)
        supervisor.is_active = False
        await session.flush()
        socket = FakeWebSocket()

        await monitor_api.stream(socket, token, None)

        assert socket.accepted is False
        assert socket.closed[0] == 1008

    async def test_a_viewer_cannot_open_a_stream(
        self, session: AsyncSession, viewer: User
    ) -> None:
        socket = FakeWebSocket()

        await monitor_api.stream(socket, token_for(viewer), None)

        assert socket.accepted is False
        assert socket.closed[0] == 1008

    async def test_a_call_stream_opens_with_a_snapshot(
        self, session: AsyncSession, supervisor: User, business: Business, call: CallLog
    ) -> None:
        session.add(
            Conversation(
                business_id=business.id,
                call_id=call.id,
                turn_index=0,
                role=SpeakerRole.AGENT,
                content="Namaste!",
            )
        )
        await session.flush()
        socket = FakeWebSocket()

        task = await run_stream(socket, token_for(supervisor), call.id)
        await asyncio.wait_for(socket.received.wait(), timeout=2)
        await stop(task)

        [snapshot] = socket.frames_of_type("snapshot")
        assert snapshot["call_id"] == str(call.id)
        assert [t["content"] for t in snapshot["data"]["turns"]] == ["Namaste!"]

    async def test_live_turns_are_relayed_to_the_watcher(
        self, session: AsyncSession, supervisor: User, business: Business, call: CallLog
    ) -> None:
        socket = FakeWebSocket()
        task = await run_stream(socket, token_for(supervisor), call.id)
        await asyncio.wait_for(socket.received.wait(), timeout=2)  # snapshot
        socket.received.clear()

        await emit(
            EventType.TRANSCRIPT_TURN,
            business.id,
            call_id=call.id,
            role=SpeakerRole.CALLER.value,
            content="Mujhe appointment chahiye",
        )
        await asyncio.wait_for(socket.received.wait(), timeout=2)
        await stop(task)

        [turn] = socket.frames_of_type(EventType.TRANSCRIPT_TURN)
        assert turn["data"]["content"] == "Mujhe appointment chahiye"

    async def test_a_pane_pinned_to_one_call_ignores_the_others(
        self, session: AsyncSession, supervisor: User, business: Business, call: CallLog
    ) -> None:
        other_call_id = uuid.uuid4()
        socket = FakeWebSocket()
        task = await run_stream(socket, token_for(supervisor), call.id)
        await asyncio.wait_for(socket.received.wait(), timeout=2)  # snapshot
        socket.received.clear()

        await emit(EventType.TRANSCRIPT_TURN, business.id, call_id=other_call_id, content="nope")
        await emit(EventType.TRANSCRIPT_TURN, business.id, call_id=call.id, content="yes")
        await asyncio.wait_for(socket.received.wait(), timeout=2)
        await stop(task)

        contents = [f["data"]["content"] for f in socket.frames_of_type(EventType.TRANSCRIPT_TURN)]
        assert contents == ["yes"]

    async def test_a_board_stream_receives_every_call_for_the_tenant(
        self, session: AsyncSession, supervisor: User, business: Business
    ) -> None:
        socket = FakeWebSocket()
        task = await run_stream(socket, token_for(supervisor), None)

        await emit(EventType.CALL_STARTED, business.id, call_id=uuid.uuid4(), status="ringing")
        await asyncio.wait_for(socket.received.wait(), timeout=2)
        await stop(task)

        assert socket.frames_of_type(EventType.CALL_STARTED)
        assert socket.frames_of_type("snapshot") == []

    async def test_another_tenants_events_never_reach_the_socket(
        self, session: AsyncSession, supervisor: User, other_business: Business
    ) -> None:
        socket = FakeWebSocket()
        task = await run_stream(socket, token_for(supervisor), None)

        await emit(EventType.CALL_STARTED, other_business.id, call_id=uuid.uuid4())
        await asyncio.sleep(0.05)
        await stop(task)

        assert socket.sent == []

    async def test_a_quiet_channel_gets_a_keepalive_ping(
        self, session: AsyncSession, supervisor: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monitor_api, "KEEPALIVE_SECONDS", 0.01)
        socket = FakeWebSocket()

        task = await run_stream(socket, token_for(supervisor), None)
        await asyncio.wait_for(socket.received.wait(), timeout=2)
        await stop(task)

        [ping] = socket.frames_of_type("ping")
        assert "at" in ping

    async def test_a_token_with_an_unparseable_subject_is_rejected(
        self, session: AsyncSession, supervisor: User
    ) -> None:
        from jose import jwt as jose_jwt

        from app.core.config import settings

        bad_token = jose_jwt.encode(
            {
                "sub": "not-a-uuid",
                "business_id": str(supervisor.business_id),
                "role": supervisor.role,
                "type": "access",
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        socket = FakeWebSocket()

        await monitor_api.stream(socket, bad_token, None)

        assert socket.accepted is False
        assert socket.closed[0] == 1008

    async def test_a_token_for_the_wrong_business_is_rejected(
        self, session: AsyncSession, supervisor: User, other_business: Business
    ) -> None:
        token, _ = create_access_token(
            user_id=supervisor.id, business_id=other_business.id, role=supervisor.role
        )
        socket = FakeWebSocket()

        await monitor_api.stream(socket, token, None)

        assert socket.accepted is False
        assert socket.closed[0] == 1008

    async def test_a_client_disconnect_ends_the_stream_quietly(
        self, session: AsyncSession, supervisor: User, business: Business
    ) -> None:
        class DisconnectingWebSocket(FakeWebSocket):
            async def send_json(self, data: dict) -> None:
                raise WebSocketDisconnect()

        socket = DisconnectingWebSocket()
        task = asyncio.create_task(monitor_api.stream(socket, token_for(supervisor), None))
        await asyncio.sleep(0)
        await emit(EventType.CALL_STARTED, business.id, call_id=uuid.uuid4())
        await asyncio.wait_for(task, timeout=2)

        # The handler returns on its own; there is nothing left to close.
        assert socket.accepted is True
        assert socket.closed is None

    async def test_an_unexpected_error_closes_the_socket_with_an_internal_error(
        self, session: AsyncSession, supervisor: User, business: Business
    ) -> None:
        class ExplodingWebSocket(FakeWebSocket):
            async def send_json(self, data: dict) -> None:
                raise RuntimeError("boom")

        socket = ExplodingWebSocket()
        task = asyncio.create_task(monitor_api.stream(socket, token_for(supervisor), None))
        await asyncio.sleep(0)
        await emit(EventType.CALL_STARTED, business.id, call_id=uuid.uuid4())
        await asyncio.wait_for(task, timeout=2)

        assert socket.closed[0] == 1011

    async def test_a_close_that_also_fails_does_not_raise(
        self, session: AsyncSession, supervisor: User, business: Business
    ) -> None:
        class AlreadyGoneWebSocket(FakeWebSocket):
            async def send_json(self, data: dict) -> None:
                raise RuntimeError("boom")

            async def close(self, code: int = 1000, reason: str | None = None) -> None:
                raise RuntimeError("already closed")

        socket = AlreadyGoneWebSocket()
        task = asyncio.create_task(monitor_api.stream(socket, token_for(supervisor), None))
        await asyncio.sleep(0)

        # Must not raise even though both the send and the close attempt fail.
        await emit(EventType.CALL_STARTED, business.id, call_id=uuid.uuid4())
        await asyncio.wait_for(task, timeout=2)

    async def test_the_relay_exits_cleanly_when_the_bus_stops_yielding(
        self, session: AsyncSession, supervisor: User
    ) -> None:
        class ExhaustedBus:
            async def publish(self, event: LiveEvent) -> None:
                return None

            async def subscribe(self, business_id: uuid.UUID):
                return
                yield  # pragma: no cover - never reached; makes this an async generator

        events_module.set_event_bus(ExhaustedBus())
        socket = FakeWebSocket()

        # The bus's stream ends immediately, so the handler should return on
        # its own rather than hang waiting for a next event.
        await asyncio.wait_for(
            monitor_api.stream(socket, token_for(supervisor), None), timeout=2
        )

        assert socket.accepted is True
        assert socket.closed is None


# --------------------------------------------------------------------------- #
# Call lifecycle events
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestLifecycleEvents:
    async def test_a_status_change_is_published(
        self, session: AsyncSession, call: CallLog, event_bus: InMemoryEventBus
    ) -> None:
        from app.services import call_service

        await call_service.apply_webhook_event(
            session,
            WebhookEvent(
                provider_call_id=call.provider_call_id,
                status=CallStatus.COMPLETED,
                duration_sec=30,
            ),
            call.provider,
        )

        ended = [e for e in event_bus.published if e.type == EventType.CALL_ENDED]
        assert len(ended) == 1
        assert ended[0].data["status"] == CallStatus.COMPLETED
        assert ended[0].data["duration_sec"] == 30

    async def test_the_callers_number_is_masked_in_published_events(
        self, session: AsyncSession, business: Business, call: CallLog, event_bus: InMemoryEventBus
    ) -> None:
        from app.services import call_service

        await call_service.hangup_call(session, business.id, call.id)

        [ended] = [e for e in event_bus.published if e.type == EventType.CALL_ENDED]
        assert call.caller_number not in ended.data["caller_number"]

    async def test_an_inbound_call_announces_itself(
        self, session: AsyncSession, phone_number, event_bus: InMemoryEventBus
    ) -> None:
        from app.services import call_service

        await call_service.handle_inbound_call(
            session,
            to_number=phone_number.number,
            from_number="+919812340000",
            provider_call_id=f"mock-{uuid.uuid4().hex[:8]}",
            provider=phone_number.provider,
        )

        assert [e.type for e in event_bus.published] == [EventType.CALL_STARTED]


@pytest.mark.asyncio
async def test_a_takeover_row_records_how_much_the_human_said(
    session: AsyncSession, supervisor: User, call: CallLog
) -> None:
    takeover = await monitoring.start_takeover(session, context_for(supervisor), call.id)
    for text in ("One moment.", "I have found your booking."):
        await monitoring.speak(session, context_for(supervisor), call.id, text)

    assert takeover.turns_spoken == 2
    released = await monitoring.end_takeover(session, context_for(supervisor), call.id)
    assert released.turns_spoken == 2
    assert ensure_utc(released.started_at) <= ensure_utc(released.ended_at)
