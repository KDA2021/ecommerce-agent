"""Confirmation grants bind one action to one buyer/session and one argument set."""
import asyncio
import hashlib
import time

import fakeredis
import pytest

from app.application.order_confirmation import (
    OrderConfirmationManager,
    RedisOrderConfirmationManager,
)


@pytest.fixture(params=["memory", "redis"])
async def manager(request):
    if request.param == "memory":
        yield OrderConfirmationManager()
    else:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        yield RedisOrderConfirmationManager(client)
        await client.aclose()


def binding():
    return {
        "action": "create_order",
        "buyer_id": "buyer-a",
        "session_id": "session-a",
        "arguments_sha256": hashlib.sha256(b'{"quantity":1}').hexdigest(),
    }


async def preview(manager, authorization=None, *, confirmed=True, token=None):
    if authorization is None:
        authorization = {**binding(), "expires_at": time.time() + 60}
    token = await manager.create_preview({"_authorization": authorization}, token=token)
    if confirmed:
        await manager.resolve(token, "confirm")
    return token


def test_action_authorization_hashes_canonical_json():
    from app.application.action_authorization import action_authorization

    left = action_authorization(
        "create_order", {"z": [1, {"b": 2, "a": "中文"}], "a": True},
        buyer_id="buyer-a", session_id="session-a",
    )
    right = action_authorization(
        "create_order", {"a": True, "z": [1, {"a": "中文", "b": 2}]},
        buyer_id="buyer-a", session_id="session-a",
    )
    canonical = '{"a":true,"z":[1,{"a":"中文","b":2}]}'.encode("utf-8")
    assert left == right == {
        "action": "create_order", "buyer_id": "buyer-a", "session_id": "session-a",
        "arguments_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    assert action_authorization(
        "create_order", {"quantity": 2}, buyer_id="buyer-a", session_id="session-a",
    )["arguments_sha256"] != binding()["arguments_sha256"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_action_authorization_rejects_non_json_numbers(value):
    from app.application.action_authorization import action_authorization

    with pytest.raises(ValueError):
        action_authorization("create_order", {"quantity": value}, buyer_id="a", session_id="s")


async def test_confirmation_grant_can_be_consumed_only_once(manager):
    token = await preview(manager)
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, binding()) is True
    assert await manager.consume(token, binding()) is False
    assert await manager.status(token) == "consumed"
    assert await manager.resolve(token, "confirm") == "consumed"
    assert await manager.wait(token, 0.01) == "timeout"


@pytest.mark.parametrize("field", ["action", "buyer_id", "session_id", "arguments_sha256"])
async def test_confirmation_grant_rejects_mismatched_binding_without_consuming(manager, field):
    token = await preview(manager)
    expected = {**binding(), field: "different"}
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, expected) is False
    assert await manager.consume(token, binding()) is True


@pytest.mark.parametrize("status", ["pending", "cancelled", "missing"])
async def test_confirmation_grant_requires_confirmed_state(manager, status):
    token = await preview(manager, confirmed=False)
    if status == "cancelled":
        await manager.resolve(token, "cancel")
    if status == "missing":
        await manager.clear(token)
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, binding()) is False


@pytest.mark.parametrize("expiry", [0, time.time() - 1, None, "future", True,
                                    float("inf"), float("nan")])
async def test_confirmation_grant_rejects_expired_or_invalid_deadline(manager, expiry):
    token = await preview(manager, {**binding(), "expires_at": expiry})
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, binding()) is False


@pytest.mark.parametrize("authorization", [{}, [], "invalid", {"expires_at": time.time() + 60}])
async def test_confirmation_grant_rejects_malformed_authorization(manager, authorization):
    token = await preview(manager, authorization)
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, binding()) is False


async def test_confirmation_grant_rejects_missing_authorization(manager):
    token = await manager.create_preview({"buyer_id": "buyer-a"})
    await manager.resolve(token, "confirm")
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, binding()) is False


@pytest.mark.parametrize("expected", [{}, None, {**binding(), "unexpected": True}])
async def test_confirmation_grant_rejects_malformed_expected_binding(manager, expected):
    token = await preview(manager)
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    assert await manager.consume(token, expected) is False


async def test_concurrent_consumption_has_one_winner(manager):
    token = await preview(manager)
    assert hasattr(manager, "consume"), "confirmation manager needs atomic consumption"
    outcomes = await asyncio.gather(*(manager.consume(token, binding()) for _ in range(20)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 19


async def test_explicit_token_collision_preserves_original_grant(manager):
    token = await preview(manager, token="fixed-token")
    payload = await manager.preview_payload(token)
    with pytest.raises(ValueError, match="token"):
        await manager.create_preview({"_authorization": {"buyer_id": "attacker"}}, token=token)
    assert await manager.preview_payload(token) == payload
    assert await manager.status(token) == "confirmed"


async def test_redis_consumption_races_across_independent_clients():
    server = fakeredis.FakeServer()
    clients = [fakeredis.aioredis.FakeRedis(server=server, decode_responses=decode)
               for decode in (True, False)]
    managers = [RedisOrderConfirmationManager(client) for client in clients]
    try:
        token = await preview(managers[0])
        assert hasattr(managers[0], "consume"), "confirmation manager needs atomic consumption"
        outcomes = await asyncio.gather(*(
            managers[index % 2].consume(token, binding()) for index in range(20)
        ))
        assert outcomes.count(True) == 1
        assert await managers[1].status(token) == "consumed"
    finally:
        for client in clients:
            await client.aclose()


async def test_redis_two_consumers_read_same_confirmed_grant_before_commit(monkeypatch):
    """Force a WATCH conflict rather than depending on scheduler timing."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = RedisOrderConfirmationManager(client)
    token = await preview(manager)
    pipeline_factory = client.pipeline
    both_read = asyncio.Event()
    reads = 0

    def synchronized_pipeline(*args, **kwargs):
        pipe = pipeline_factory(*args, **kwargs)
        original_hmget = pipe.hmget

        async def synchronized_hmget(*fields):
            nonlocal reads
            result = await original_hmget(*fields)
            reads += 1
            if reads == 2:
                both_read.set()
            await both_read.wait()
            return result

        monkeypatch.setattr(pipe, "hmget", synchronized_hmget)
        return pipe

    monkeypatch.setattr(client, "pipeline", synchronized_pipeline)
    try:
        outcomes = await asyncio.wait_for(asyncio.gather(
            manager.consume(token, binding()), manager.consume(token, binding()),
        ), timeout=2)
        assert sorted(outcomes) == [False, True]
        assert reads == 3  # The conflicted transaction retries and observes consumed.
    finally:
        await client.aclose()


async def test_redis_concurrent_explicit_token_creation_has_one_winner():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = RedisOrderConfirmationManager(client)
    try:
        outcomes = await asyncio.gather(*(
            manager.create_preview({"buyer_id": str(index)}, token="fixed-token")
            for index in range(10)
        ), return_exceptions=True)
        assert outcomes.count("fixed-token") == 1
        assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 9
    finally:
        await client.aclose()
