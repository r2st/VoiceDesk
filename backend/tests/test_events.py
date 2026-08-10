"""The live-event bus: channel naming, the Redis-backed bus, and the registry."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.events import (
    CHANNEL_PREFIX,
    EventType,
    InMemoryEventBus,
    LiveEvent,
    RedisEventBus,
    channel_for,
    get_event_bus,
    set_event_bus,
)
from tests.fakes import FakePubSub, FakeRedis


def test_channel_for_is_namespaced_per_tenant() -> None:
    business_id = uuid.uuid4()
    assert channel_for(business_id) == f"{CHANNEL_PREFIX}{business_id}"


def test_get_event_bus_defaults_to_redis() -> None:
    set_event_bus(None)
    try:
        assert isinstance(get_event_bus(), RedisEventBus)
    finally:
        set_event_bus(None)


def test_in_memory_bus_clear_drops_recorded_events() -> None:
    bus = InMemoryEventBus()
    bus.published.append(LiveEvent(type=EventType.CALL_STARTED, business_id=uuid.uuid4()))

    bus.clear()

    assert bus.published == []


async def _wait_until_subscribed(fake_redis: FakeRedis, channel: str) -> None:
    """Spin until the fake's subscriber list for ``channel`` is non-empty.

    ``RedisEventBus.subscribe`` is an async generator: nothing in its body
    runs until its consumer task is scheduled. This gives that task room to
    reach its first real suspension point (``queue.get()``) before the test
    publishes, without depending on real wall-clock timing.
    """
    for _ in range(100):
        if fake_redis._subscribers.get(channel):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"nothing subscribed to {channel} in time")


class TestRedisEventBus:
    async def test_publish_puts_the_event_on_the_tenant_channel(
        self, fake_redis: FakeRedis
    ) -> None:
        business_id = uuid.uuid4()
        bus = RedisEventBus()
        pubsub = fake_redis.pubsub()
        await pubsub.subscribe(channel_for(business_id))

        await bus.publish(LiveEvent(type=EventType.CALL_STARTED, business_id=business_id))

        message = await pubsub.get_message(timeout=1)
        assert message is not None
        assert LiveEvent.from_json(message["data"]).type == EventType.CALL_STARTED

    async def test_subscribe_yields_published_events(self, fake_redis: FakeRedis) -> None:
        business_id = uuid.uuid4()
        bus = RedisEventBus()
        stream = bus.subscribe(business_id)
        next_event = asyncio.ensure_future(anext(stream))
        await _wait_until_subscribed(fake_redis, channel_for(business_id))

        await bus.publish(LiveEvent(type=EventType.CALL_ENDED, business_id=business_id))

        received = await next_event
        assert received.type == EventType.CALL_ENDED
        await stream.aclose()  # type: ignore[attr-defined]

    async def test_subscribe_skips_malformed_messages(self, fake_redis: FakeRedis) -> None:
        business_id = uuid.uuid4()
        channel = channel_for(business_id)
        bus = RedisEventBus()
        stream = bus.subscribe(business_id)
        next_event = asyncio.ensure_future(anext(stream))
        await _wait_until_subscribed(fake_redis, channel)

        await fake_redis.publish(channel, "not-json")
        await fake_redis.publish(
            channel, LiveEvent(type=EventType.HANDOFF, business_id=business_id).to_json()
        )

        received = await next_event
        assert received.type == EventType.HANDOFF
        await stream.aclose()  # type: ignore[attr-defined]

    async def test_subscribe_polls_again_when_nothing_arrives(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``get_message`` timeout is not an event; it just loops for the next poll."""
        monkeypatch.setattr("app.core.events.POLL_INTERVAL_SECONDS", 0.01)
        business_id = uuid.uuid4()
        channel = channel_for(business_id)
        bus = RedisEventBus()
        stream = bus.subscribe(business_id)
        next_event = asyncio.ensure_future(anext(stream))
        await _wait_until_subscribed(fake_redis, channel)

        # Outlast the poll interval so the first get_message() call times out
        # and the loop goes around for a second poll before this arrives.
        await asyncio.sleep(0.03)
        await bus.publish(LiveEvent(type=EventType.CALL_STATUS, business_id=business_id))

        received = await next_event
        assert received.type == EventType.CALL_STATUS
        await stream.aclose()  # type: ignore[attr-defined]

    async def test_subscribe_recovers_from_a_dropped_connection(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken Redis connection is retried, not left to kill every socket."""
        monkeypatch.setattr("app.core.events.RECONNECT_BACKOFF_SECONDS", 0.001)
        monkeypatch.setattr("app.core.events.POLL_INTERVAL_SECONDS", 0.01)

        original_get_message = FakePubSub.get_message
        state = {"raised": False}

        async def flaky_get_message(self, **kwargs):
            if not state["raised"]:
                state["raised"] = True
                raise ConnectionError("simulated Redis blip")
            return await original_get_message(self, **kwargs)

        monkeypatch.setattr(FakePubSub, "get_message", flaky_get_message)

        business_id = uuid.uuid4()
        channel = channel_for(business_id)
        bus = RedisEventBus()
        stream = bus.subscribe(business_id)
        next_event = asyncio.ensure_future(anext(stream))

        # Wait for the failure to be hit and a second subscription to replace it.
        for _ in range(200):
            if state["raised"] and len(fake_redis._subscribers.get(channel, [])) >= 2:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("subscription never recovered from the dropped connection")

        await bus.publish(LiveEvent(type=EventType.CALL_STARTED, business_id=business_id))

        received = await asyncio.wait_for(next_event, timeout=2)
        assert received.type == EventType.CALL_STARTED
        await stream.aclose()  # type: ignore[attr-defined]

    async def test_subscribe_unsubscribes_when_the_consumer_stops(
        self, fake_redis: FakeRedis
    ) -> None:
        business_id = uuid.uuid4()
        channel = channel_for(business_id)
        bus = RedisEventBus()
        stream = bus.subscribe(business_id)
        next_event = asyncio.ensure_future(anext(stream))
        await _wait_until_subscribed(fake_redis, channel)

        next_event.cancel()
        try:
            await next_event
        except asyncio.CancelledError:
            pass
        await stream.aclose()  # type: ignore[attr-defined]

        assert fake_redis._subscribers.get(channel) == []
