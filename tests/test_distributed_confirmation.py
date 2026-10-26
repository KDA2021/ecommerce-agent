"""A preview created in a queue worker must be confirmable in a web process."""
import asyncio

import fakeredis

from app.application import order_confirmation


async def test_two_confirmation_instances_share_first_decision_and_owner():
    server = fakeredis.FakeServer()
    worker_redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    web_redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    assert hasattr(order_confirmation, "RedisOrderConfirmationManager")
    cls = order_confirmation.RedisOrderConfirmationManager
    worker, web = cls(worker_redis), cls(web_redis)
    token = await worker.create_preview({"buyer_id": "alice", "items": []})
    assert (await web.preview_payload(token))["buyer_id"] == "alice"
    waiting = asyncio.create_task(worker.wait(token, 2))
    assert await web.resolve(token, "confirm") == "confirmed"
    assert await web.resolve(token, "cancel") == "confirmed"
    assert await waiting == "confirmed"
    await worker.clear(token)
    await worker_redis.aclose()
    await web_redis.aclose()


async def test_confirm_winning_at_timeout_boundary_is_not_overwritten():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cls = order_confirmation.RedisOrderConfirmationManager
    worker, web = cls(client), cls(client)
    token = await worker.create_preview({"buyer_id": "alice"})
    status = worker.status

    async def confirm_between_status_read_and_deadline(_token):
        previous = await status(_token)
        assert await web.resolve(_token, "confirm") == "confirmed"
        await asyncio.sleep(0.02)
        return previous

    worker.status = confirm_between_status_read_and_deadline
    assert await worker.wait(token, 0.01) == "confirmed"
    assert await web.status(token) == "confirmed"
    await client.aclose()
