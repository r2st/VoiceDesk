"""Fan-out of live call events to connected dashboards (design doc §4.3).

Supervisors watch calls from the dashboard over a WebSocket, but the turns they
want to see are produced by whichever API replica happens to be handling that
call's media pipeline. Redis pub/sub bridges the two: producers publish to a
per-tenant channel and every replica holding a socket for that tenant relays
what it receives.

Publishing is best effort by design. A supervisor's view going stale is a much
smaller problem than a live phone call failing, so a broker outage degrades
monitoring rather than breaking the call path.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

CHANNEL_PREFIX = "voicedesk:events:"

#: How long a subscriber waits for a message before yielding control so it can
#: send a keepalive and notice that its client has gone away.
POLL_INTERVAL_SECONDS = 0.5


class EventType:
    """Event names on the wire. Clients switch on these, so they are stable."""

    CALL_STARTED = "call.started"
    CALL_STATUS = "call.status"
    CALL_ENDED = "call.ended"
    TRANSCRIPT_TURN = "transcript.turn"
    TAKEOVER_STARTED = "takeover.started"
    TAKEOVER_ENDED = "takeover.ended"
    HANDOFF = "call.handoff"


@dataclass(slots=True)
class LiveEvent:
    """One thing that happened on a call, addressed to a tenant's watchers."""

    type: str
    business_id: uuid.UUID
    call_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)
    emitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "business_id": str(self.business_id),
            "call_id": str(self.call_id) if self.call_id else None,
            "data": self.data,
            "emitted_at": self.emitted_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: str) -> LiveEvent:
        payload = json.loads(raw)
        return cls(
            type=payload["type"],
            business_id=uuid.UUID(payload["business_id"]),
            call_id=uuid.UUID(payload["call_id"]) if payload.get("call_id") else None,
            data=payload.get("data") or {},
            emitted_at=datetime.fromisoformat(payload["emitted_at"]),
        )


def channel_for(business_id: uuid.UUID) -> str:
    """One channel per tenant.

    Per-call channels would cut the fan-out, but they would also mean a
    subscriber has to re-subscribe every time a new call starts. Watchers
    filter by ``call_id`` instead; tenant-level volume is small enough that the
    extra messages cost less than the churn would.
    """
    return f"{CHANNEL_PREFIX}{business_id}"


class EventBus(Protocol):
    async def publish(self, event: LiveEvent) -> None: ...

    def subscribe(self, business_id: uuid.UUID) -> AsyncIterator[LiveEvent]:
        """Async iterator over this tenant's events, starting from now."""
        ...  # pragma: no cover - structural stub; implementations don't inherit this body


class RedisEventBus:
    """Production bus. Survives multiple API replicas."""

    async def publish(self, event: LiveEvent) -> None:
        try:
            await get_redis().publish(channel_for(event.business_id), event.to_json())
        except Exception as exc:  # pragma: no cover - depends on Redis being down
            logger.warning("Dropping live event %s: %s", event.type, exc)

    async def subscribe(self, business_id: uuid.UUID) -> AsyncIterator[LiveEvent]:
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(channel_for(business_id))
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=POLL_INTERVAL_SECONDS
                )
                if message is None:
                    continue
                try:
                    yield LiveEvent.from_json(message["data"])
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    # A malformed message is one bad publisher, not a reason to
                    # tear down every dashboard watching this tenant.
                    logger.warning("Skipping unreadable live event: %s", exc)
        finally:
            await pubsub.unsubscribe(channel_for(business_id))
            await pubsub.aclose()


class InMemoryEventBus:
    """Single-process bus, used by tests and by a local dev server.

    Each subscriber gets its own queue, so a slow reader cannot starve the
    others. Queues are bounded: a subscriber that stops draining drops the
    oldest events rather than growing without limit.
    """

    def __init__(self, max_queue: int = 256) -> None:
        self._queues: dict[uuid.UUID, list[asyncio.Queue[LiveEvent]]] = {}
        self._max_queue = max_queue
        #: Everything published, in order — assertions read this.
        self.published: list[LiveEvent] = []

    async def publish(self, event: LiveEvent) -> None:
        self.published.append(event)
        for queue in list(self._queues.get(event.business_id, [])):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    async def subscribe(self, business_id: uuid.UUID) -> AsyncIterator[LiveEvent]:
        queue: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=self._max_queue)
        self._queues.setdefault(business_id, []).append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._queues.get(business_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

    def clear(self) -> None:
        self.published.clear()


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = RedisEventBus()
    return _bus


def set_event_bus(bus: EventBus | None) -> None:
    """Override the shared bus (tests inject the in-memory one)."""
    global _bus
    _bus = bus


async def emit(
    event_type: str,
    business_id: uuid.UUID,
    *,
    call_id: uuid.UUID | None = None,
    **data: Any,
) -> LiveEvent:
    """Publish one event. Never raises — see the module docstring."""
    event = LiveEvent(type=event_type, business_id=business_id, call_id=call_id, data=data)
    try:
        await get_event_bus().publish(event)
    except Exception as exc:  # pragma: no cover - the bus already logs its own
        logger.warning("Live event %s could not be published: %s", event_type, exc)
    return event


@asynccontextmanager
async def watch(business_id: uuid.UUID) -> AsyncIterator[AsyncIterator[LiveEvent]]:
    """Subscribe for the duration of a block, closing the subscription after."""
    stream = get_event_bus().subscribe(business_id)
    try:
        yield stream
    finally:
        await stream.aclose()  # type: ignore[attr-defined]
