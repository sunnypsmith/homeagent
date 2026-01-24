import pytest

from home_agent.core.events import EventBus


@pytest.mark.asyncio
async def test_event_bus_publish_subscribe() -> None:
    bus = EventBus()
    seen = []

    async def handler(evt):
        seen.append((evt.topic, evt.payload))

    await bus.subscribe("x", handler)
    await bus.publish("x", {"a": 1})
    assert seen == [("x", {"a": 1})]

