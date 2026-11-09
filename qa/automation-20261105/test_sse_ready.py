import asyncio

from app.infrastructure.eventbroker import EventBroker


def test_sse_has_first_byte_before_any_business_request(commerce):
    with commerce.alice.get(
        commerce.base + "/commerce/events",
        params={"shopping_session_id": "qa-ready"},
        stream=True,
        timeout=(2, 1),
    ) as response:
        assert response.status_code == 200
        first = next(response.iter_lines(chunk_size=1))
        assert first == b": connected"
    assert commerce.state()["orders"] == []


async def test_ready_subscription_registers_before_comment_and_cleans_up():
    broker = EventBroker()
    stream = broker.subscribe_ready("qa-ready")
    assert await anext(stream) == ": connected\n\n"
    assert len(broker._queues["qa-ready"]) == 1
    broker.publish("qa-ready", "final.result", {"reply": "ready"})
    assert '"reply": "ready"' in await anext(stream)
    await stream.aclose()
    assert not broker._queues["qa-ready"]


async def test_legacy_subscribe_still_starts_with_business_event():
    broker = EventBroker()
    stream = broker.subscribe("qa-legacy")
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    broker.publish("qa-legacy", "final.result", {"reply": "legacy"})
    assert (await pending).startswith("event: final.result\n")
    await stream.aclose()
