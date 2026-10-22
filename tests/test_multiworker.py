# -*- coding: utf-8 -*-
"""P11-4 多 worker 队列与广播测试。

全部用 ``fakeredis`` 注入，不依赖真实 Redis / MySQL：

- Redis Stream：入队 → 消费 → ack、pending 重投、状态回写、fail loud/soft；
- Pub/Sub 背板：跨实例广播（两个独立 client 模拟两个 worker 进程）；
- EventBroker：direct 模式行为不变 + 背板 fanout 只写本地队列；
- server：``QUEUE_MODE=redis`` 入队返回 task_id + 状态查询；
- worker：``process_task`` 写状态 / 发 final.result / 失败发 error。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.domain.queue.ports.task_queue import IntentTask, TaskStatus
from app.infrastructure.eventbackplane import RedisEventBackplane
from app.infrastructure.eventbroker import EventBroker
from app.infrastructure.queue.redis_stream_queue import (
    QueueTaskError,
    RedisStreamTaskQueue,
)


def _task(task_id: str = "task-1", session_id: str = "s1", **overrides) -> IntentTask:
    """构造测试用任务。"""
    payload = {
        "task_id": task_id,
        "shopping_session_id": session_id,
        "buyer_id": "buyer-1",
        "locale": "zh-CN",
        "currency": "CNY",
        "raw_query": "推荐一个旅行三件套",
    }
    payload.update(overrides)
    return IntentTask(**payload)


def _redis(server=None) -> fakeredis.aioredis.FakeRedis:
    """新建一个 fakeredis 客户端（decode_responses=True）。

    多个 client 共用一个 ``FakeServer`` 才能模拟“多个 worker 进程连同一个 Redis”；
    不传 server 时 fakeredis 会给每个 client 独立内存库，跨实例广播就测不出来了。
    """
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def client() -> fakeredis.aioredis.FakeRedis:
    """默认 fakeredis 客户端（自动 aclose）。"""
    return _redis()


@pytest.fixture
def queue(client) -> RedisStreamTaskQueue:
    """注入 fakeredis 的队列（消费者名固定，便于断言）。"""
    return RedisStreamTaskQueue("", client=client, consumer="worker-1")


def _run(coro):
    """在同步测试里跑一段协程（fakeredis 内存态与事件循环无关）。"""
    return asyncio.run(coro)


async def _next_frame(iterator, timeout: float = 2.0):
    """从 SSE 异步生成器取一帧，超时抛 AssertionError。"""
    return await asyncio.wait_for(anext(iterator), timeout=timeout)


async def _wait_until(predicate, timeout: float = 2.0, message: str = "条件未满足") -> None:
    """轮询等待谓词成立（异步测试里的确定性同步点）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


async def _read_one(backplane, session_id: str, sink: list) -> None:
    """在独立任务里消费背板统一事件流（避免「先 publish 后订阅」的竞态）。"""
    async for remote_session, event_type, payload in backplane.remote_events():
        if remote_session == session_id:
            sink.append((event_type, payload))
            return


async def _start_broker_reader(broker, session_id: str):
    """在后台任务里消费 broker 的 SSE 帧（先启动，再 publish）。

    异步生成器的 body 不会在 ``subscribe()`` 时执行，必须有人先 ``anext``；
    这里返回 ``(iterator, task)``，调用方 publish 之后 await 这个 task 取帧。
    """
    iterator = broker.subscribe(session_id)
    task = asyncio.create_task(anext(iterator))
    # 让任务跑到 ``await queue.get()``（此时订阅已注册进 broker._queues）。
    await _wait_until(
        lambda: bool(broker._queues.get(session_id)),
        message=f"broker 订阅未注册：{session_id}",
    )
    return iterator, task


# ======================================================================
# 1. Redis Stream 队列
# ======================================================================
async def test_enqueue_and_consume_roundtrip(queue):
    task = _task(experiment_config={"model_route_mode": "direct"})

    message_id = await queue.enqueue(task)
    assert message_id
    assert await queue.depth() == 1

    # 读走一条：返回完整任务值对象（含嵌套 experiment_config）。
    read = await queue.read_new(count=1)
    assert len(read) == 1
    read_id, consumed = read[0]
    assert read_id == message_id
    assert consumed == task
    assert consumed.experiment_config == {"model_route_mode": "direct"}

    # 再读就没有了（短轮询不阻塞）。
    assert await queue.read_new(count=1) == []


async def test_read_without_ack_keeps_message_pending(queue):
    await queue.enqueue(_task("task-pending"))
    (message_id, _) = (await queue.read_new(count=1))[0]

    # 读走不等于出队：消息仍在 Stream 里（XACK 后才真正确认），且进入 PEL。
    assert await queue.depth() == 1

    pending = await queue.read_pending(count=10)
    assert [row[0] for row in pending] == [message_id]
    pending_id, pending_task, deliveries = pending[0]
    assert pending_task.task_id == "task-pending"
    assert deliveries >= 1

    assert await queue.ack(message_id) is True
    assert await queue.read_pending(count=10) == []
    # 消息本体仍在 Stream（消费者组模式下 ack 只清 PEL，不删消息）。
    assert await queue.depth() == 1


async def test_ack_is_false_for_unknown_message(queue):
    assert await queue.ack("0-0") is False


async def test_claim_stale_redelivers_unacked_message(client):
    """worker A 取走消息后崩溃：worker B 用 XAUTOCLAIM 把消息重投给自己。"""
    queue_a = RedisStreamTaskQueue("", client=client, consumer="worker-a")
    queue_b = RedisStreamTaskQueue("", client=client, consumer="worker-b")
    await queue_a.enqueue(_task("task-stale"))

    (message_id, _) = (await queue_a.read_new(count=1))[0]

    # 未 ack 的消息不会投给别的消费者；只有 claim 之后才会。
    assert await queue_b.read_new(count=1) == []
    await asyncio.sleep(0.05)

    claimed = await queue_b.claim_stale(min_idle_ms=10, count=10)
    assert claimed, "XAUTOCLAIM 应把超时未 ack 的消息交给 worker-b"
    claimed_id, claimed_task, deliveries = claimed[0]
    assert claimed_id == message_id
    assert claimed_task.task_id == "task-stale"
    assert deliveries >= 1

    # 重投后 worker-b 能 ack 掉（PEL 里这条属于 worker-b 了）。
    assert await queue_b.ack(claimed_id) is True
    assert await queue_b.read_pending(count=10) == []


async def test_claim_stale_ignores_fresh_messages(queue):
    await queue.enqueue(_task("task-fresh"))
    await queue.read_new(count=1)

    # 空闲阈值很大 → 不该抢别人的消息。
    assert await queue.claim_stale(min_idle_ms=600_000, count=10) == []


async def test_claim_stale_falls_back_to_pending(queue, monkeypatch):
    """Redis 客户端不支持 XAUTOCLAIM 时退化为 XPENDING 巡检 + XCLAIM 重投。"""

    async def _boom(*_args, **_kwargs):
        raise Exception("unknown command XAUTOCLAIM")

    monkeypatch.setattr(queue.client, "xautoclaim", _boom)
    await queue.enqueue(_task("task-fallback"))
    (message_id, _) = (await queue.read_new(count=1))[0]
    await asyncio.sleep(0.05)

    claimed = await queue.claim_stale(min_idle_ms=10, count=10)
    assert len(claimed) == 1
    claimed_id, task, deliveries = claimed[0]
    assert claimed_id == message_id  # 兜底路径不新增 Stream entry
    assert task.task_id == "task-fallback"
    assert deliveries >= 1
    # 消息没有因为重投被复制（Stream 长度仍为 1）。
    assert await queue.depth() == 1
    # PEL 仍在消费者组里，ack 后才清空。
    assert [row[0] for row in await queue.read_pending(count=10)] == [message_id]
    assert await queue.ack(message_id) is True
    assert await queue.read_pending(count=10) == []


async def test_claim_stale_falls_back_to_reinsert(queue, monkeypatch):
    """XAUTOCLAIM 与 XCLAIM 都不可用时，重新入队兜底（XDEL 旧 entry，长度守恒）。"""

    async def _boom(*_args, **_kwargs):
        raise Exception("unsupported")

    monkeypatch.setattr(queue.client, "xautoclaim", _boom)
    monkeypatch.setattr(queue.client, "xclaim", _boom)
    await queue.enqueue(_task("task-reread"))
    (old_id, _) = (await queue.read_new(count=1))[0]
    await asyncio.sleep(0.05)

    claimed = await queue.claim_stale(min_idle_ms=10, count=10)
    assert len(claimed) == 1
    new_id, task, deliveries = claimed[0]
    assert task.task_id == "task-reread"
    assert new_id != old_id
    assert deliveries >= 1
    # 旧 entry 已删除：Stream 长度守恒，不会越重投越长。
    assert await queue.depth() == 1
    # 这条新 entry 还没被消费，可以正常读出来。
    read = await queue.read_new(count=1)
    assert [row[0] for row in read] == [new_id]


async def test_status_roundtrip_and_unknown(queue):
    assert await queue.get_status("missing") is None

    await queue.set_status(
        TaskStatus(task_id="task-1", state="done", final_text="旅行三件套推荐如下"),
    )
    status = await queue.get_status("task-1")
    assert status == TaskStatus(task_id="task-1", state="done", final_text="旅行三件套推荐如下")

    await queue.set_status(TaskStatus(task_id="task-2", state="failed", error="boom"))
    assert (await queue.get_status("task-2")).error == "boom"


async def test_status_key_prefix_and_ttl(queue, client):
    await queue.set_status(TaskStatus(task_id="task-1", state="queued"))
    assert await client.exists("ecommerce:task:task-1") == 1
    assert 0 < await client.ttl("ecommerce:task:task-1") <= 4 * 60 * 60


async def test_enqueue_without_redis_raises():
    disabled = RedisStreamTaskQueue("")
    assert disabled.enabled is False
    with pytest.raises(QueueTaskError):
        await disabled.enqueue(_task())


async def test_read_and_status_fail_soft_when_disabled():
    disabled = RedisStreamTaskQueue("")
    assert await disabled.read_new(count=1) == []
    assert await disabled.read_pending(count=1) == []
    assert await disabled.claim_stale(min_idle_ms=1, count=1) == []
    assert await disabled.get_status("task-1") is None
    assert await disabled.depth() == 0
    assert await disabled.ensure_ready() is False
    await disabled.close()  # 幂等


async def test_ensure_ready_creates_group_and_is_idempotent(queue, client):
    assert await queue.ensure_ready() is True
    assert await queue.ensure_ready() is True  # BUSYGROUP 被吞掉，不抛
    groups = await client.xinfo_groups("ecommerce:intents")
    assert groups[0]["name"] == "ecommerce-workers"


async def test_duplicate_consumers_compete_for_same_group(client):
    """同一个消费者组内，两个消费者不会各自拿到同一条任务。"""
    queue_a = RedisStreamTaskQueue("", client=client, consumer="a")
    queue_b = RedisStreamTaskQueue("", client=client, consumer="b")
    await queue_a.enqueue(_task("task-x"))

    first = await queue_a.read_new(count=1)
    second = await queue_b.read_new(count=1)
    assert len(first) == 1
    assert second == []


# ======================================================================
# 2. Pub/Sub 事件背板
# ======================================================================
async def test_backplane_broadcasts_across_instances():
    """A 实例 publish → B 实例（独立 client，模拟另一个 worker 进程）收到。"""
    server = fakeredis.FakeServer()
    publisher = RedisEventBackplane("", client=_redis(server))
    subscriber = RedisEventBackplane("", client=_redis(server))
    received: list = []

    assert await subscriber.start() is True
    reader = asyncio.create_task(_read_one(subscriber, "s1", received))
    try:
        await asyncio.sleep(0.2)  # 让 reader 任务跑起来（订阅已在 start() 完成）
        assert await publisher.publish("s1", "final.result", {"reply": "来自 A"}) is True
        await asyncio.wait_for(reader, timeout=3.0)
        assert received == [("final.result", {"reply": "来自 A"})]
    finally:
        reader.cancel()
        await publisher.close()
        await subscriber.close()


async def test_backplane_ignores_other_sessions():
    server = fakeredis.FakeServer()
    publisher = RedisEventBackplane("", client=_redis(server))
    subscriber = RedisEventBackplane("", client=_redis(server))
    received: list = []
    assert await subscriber.start() is True
    reader = asyncio.create_task(_read_one(subscriber, "s1", received))
    try:
        await asyncio.sleep(0.2)  # 让 reader 任务跑起来（订阅已在 start() 完成）
        await publisher.publish("other", "final.result", {"reply": "不是我的"})
        await publisher.publish("s1", "final.result", {"reply": "是我的"})
        await asyncio.wait_for(reader, timeout=3.0)
        assert received == [("final.result", {"reply": "是我的"})]
    finally:
        reader.cancel()
        await publisher.close()
        await subscriber.close()


async def test_backplane_disabled_is_noop():
    backplane = RedisEventBackplane("")
    assert backplane.enabled is False
    assert await backplane.start() is False
    assert await backplane.publish("s1", "final.result", {"reply": "x"}) is False
    frames = []
    async for frame in backplane.listen("s1"):
        frames.append(frame)
    assert frames == []
    await backplane.close()


async def test_backplane_channel_name():
    backplane = RedisEventBackplane("", client=_redis())
    assert backplane.channel_for("s1") == "ecommerce:events:s1"
    await backplane.close()


# ======================================================================
# 3. EventBroker：direct 行为不变 + 背板 fanout
# ======================================================================
async def test_eventbroker_direct_mode_unchanged():
    """direct 模式（无背板）：行为与 P11-3 完全一致。"""
    broker = EventBroker()
    assert broker.backplane is None
    # 无背板时 start / stop / aclose 都是 no-op，不影响既有行为。
    assert await broker.start() is False
    await broker.stop()
    await broker.aclose()

    # 订阅端必须在 publish 之前真正跑起来（异步生成器首次 anext 才注册队列）。
    iterator, reader = await _start_broker_reader(broker, "s1")
    broker.publish("s1", "final.result", {"reply": "本地"})
    frame = await asyncio.wait_for(reader, timeout=2.0)
    await iterator.aclose()
    assert frame == 'event: final.result\ndata: {"reply": "本地"}\n\n'


async def test_eventbroker_fans_out_backplane_events_to_local_sse():
    """其他实例发布的事件，要能写进本进程的 SSE 订阅队列。"""
    server = fakeredis.FakeServer()
    listener = RedisEventBackplane("", client=_redis(server))
    publisher = RedisEventBackplane("", client=_redis(server))
    broker = EventBroker(backplane=listener)

    assert await broker.start() is True
    iterator, reader = await _start_broker_reader(broker, "s1")
    try:
        await publisher.publish("s1", "final.result", {"reply": "其他实例的回复"})
        frame = await asyncio.wait_for(reader, timeout=3.0)
        assert "event: final.result" in frame
        assert "其他实例的回复" in frame
    finally:
        await iterator.aclose()
        await broker.aclose()
        await publisher.close()


async def test_eventbroker_does_not_echo_own_events():
    """本进程 publish 只写本地队列 + 背板一次，不把收到的帧再广播（防风暴）。"""
    published: list = []

    class _Backplane:
        enabled = True

        async def publish(self, session_id, event_type, payload):
            published.append((session_id, event_type, payload))
            return True

        async def remote_events(self):
            await asyncio.sleep(3600)
            if False:  # pragma: no cover - 只为了让本方法保持 async generator
                yield ("", "", {})

        async def close(self):
            return None

    broker = EventBroker(backplane=_Backplane())
    iterator, reader = await _start_broker_reader(broker, "s1")
    broker.publish("s1", "token.delta", {"token": "你"})
    frame = await asyncio.wait_for(reader, timeout=2.0)
    await asyncio.sleep(0)  # 让 create_task 的背板 publish 跑起来
    await iterator.aclose()
    await broker.stop()

    assert "token.delta" in frame
    assert published == [("s1", "token.delta", {"token": "你"})]


# ======================================================================
# 4. server：QUEUE_MODE=redis
# ======================================================================
class _FakeBroker:
    def __init__(self):
        self.events: list = []

    def publish(self, session_id, event_type, payload):
        self.events.append((session_id, event_type, payload))


class _FakeAgent:
    def __init__(self, reply: str = "测试回复"):
        self.reply = reply

    async def ainvoke(self, payload, config):
        return {"messages": [AIMessage(content=self.reply)]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})


class _FakeContainer:
    """只给 /commerce/intents 需要的最小面；task_queue 决定 direct / redis 分支。"""

    def __init__(self, task_queue=None):
        self.agent = _FakeAgent()
        self.broker = _FakeBroker()
        self.semantic_cache = _FakeCache()
        self.preference_store = _FakePreferenceStore()
        self.task_queue = task_queue
        self.experiment_config = {"model_route_mode": "direct"}


class _FakeCache:
    async def lookup(self, *args, **kwargs):
        return None

    async def remember(self, *args, **kwargs):
        return None


class _FakePreferenceStore:
    async def list_by_buyer(self, buyer_id):
        return []


def _client(container) -> TestClient:
    from app.presentation.server import create_app

    return TestClient(create_app(container=container, build_on_startup=False))


def _payload(**overrides) -> dict:
    body = {
        "shopping_session_id": "s-test",
        "buyer_id": "buyer-1",
        "query": "推荐一个旅行三件套",
    }
    body.update(overrides)
    return body


def test_intents_enqueues_and_returns_task_id_in_redis_mode(queue):
    container = _FakeContainer(task_queue=queue)
    client = _client(container)
    response = client.post(
        "/commerce/intents",
        json=_payload(preference_hint={"budget_max_major": 200, "currency": "CNY"}),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"]
    assert body["reply"] == ""
    # 请求线程只收单：没跑 Agent、没有直接发 SSE 事件。
    assert container.broker.events == []

    # 任务真的进了 Stream：用同库的新 client 读出（不干扰请求侧事件循环）。
    reader = RedisStreamTaskQueue("", client=queue.client, consumer="reader")
    read = _run(reader.read_new(count=1))
    assert len(read) == 1
    _, task = read[0]
    assert task.task_id == body["task_id"]
    assert task.raw_query == "推荐一个旅行三件套"
    assert "预算上限 200 CNY" in task.experiment_config["preference_directive"]

    # 状态查询接口能回读 queued。
    status = client.get(f"/commerce/intents/{body['task_id']}").json()
    assert status == {
        "task_id": body["task_id"],
        "state": "queued",
        "found": True,
        "final_text": "",
        "error": "",
        "queue_position": 0,
    }


def test_task_status_unknown_returns_found_false(queue):
    container = _FakeContainer(task_queue=queue)
    client = _client(container)
    status = client.get("/commerce/intents/does-not-exist").json()
    assert status["found"] is False
    assert status["state"] == "unknown"


def test_task_status_returns_done_with_final_text(queue):
    _run(queue.set_status(TaskStatus(task_id="t-done", state="done", final_text="最终回复")))
    container = _FakeContainer(task_queue=queue)
    status = _client(container).get("/commerce/intents/t-done").json()
    assert status["state"] == "done"
    assert status["final_text"] == "最终回复"


def test_intents_returns_503_when_queue_unavailable():
    container = _FakeContainer(task_queue=RedisStreamTaskQueue(""))
    response = _client(container).post("/commerce/intents", json=_payload())
    assert response.status_code == 503
    assert "任务队列未就绪" in response.json()["detail"]


def test_intents_direct_mode_still_runs_agent():
    container = _FakeContainer(task_queue=None)
    response = _client(container).post("/commerce/intents", json=_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "测试回复"
    assert body["task_id"] == ""
    assert body["status"] == ""
    assert container.broker.events == [("s-test", "final.result", {"reply": "测试回复"})]


# ======================================================================
# 5. scripts/agent_worker.py
# ======================================================================
def _worker_container(broker, *, metrics_store=None, task_store=None):
    """给 worker 的 process_task 用最小容器替身。"""
    return SimpleNamespace(
        agent=_FakeAgent("worker 回复"),
        broker=broker,
        metrics_store=metrics_store,
        task_store=task_store,
        preference_store=_FakePreferenceStore(),
    )


async def test_worker_processes_task_and_writes_done_status(queue):
    from scripts.agent_worker import process_task

    broker = EventBroker()
    iterator, reader = await _start_broker_reader(broker, "s1")

    await queue.enqueue(_task("task-1"))
    (message_id, task) = (await queue.read_new(count=1))[0]

    status = await process_task(
        task, container=_worker_container(broker), broker=broker, task_queue=queue,
    )
    assert status.state == "done"
    assert status.final_text == "worker 回复"

    stored = await queue.get_status("task-1")
    assert stored.state == "done"
    assert stored.final_text == "worker 回复"

    started = await asyncio.wait_for(reader, timeout=2.0)
    assert "event: task.started" in started
    assert "task-1" in started
    final = await _next_frame(iterator)
    assert "event: final.result" in final
    assert "worker 回复" in final
    await iterator.aclose()

    await queue.ack(message_id)
    assert await queue.read_pending(count=10) == []


async def test_worker_records_failure_and_publishes_error(queue):
    from scripts.agent_worker import process_task

    class _BoomAgent:
        async def ainvoke(self, payload, config):
            raise RuntimeError("模型超时")

    broker = EventBroker()
    container = _worker_container(broker)
    container.agent = _BoomAgent()
    iterator, reader = await _start_broker_reader(broker, "s-boom")

    await queue.enqueue(_task("task-boom", session_id="s-boom"))
    (_, task) = (await queue.read_new(count=1))[0]

    status = await process_task(
        task, container=container, broker=broker, task_queue=queue,
    )
    assert status.state == "failed"
    assert "模型超时" in status.error

    stored = await queue.get_status("task-boom")
    assert stored.state == "failed"
    assert "模型超时" in stored.error

    # 失败也要广播 error 事件，客户端才能停止等待。
    await asyncio.wait_for(reader, timeout=2.0)  # task.started
    error_frame = await _next_frame(iterator)
    assert "event: error" in error_frame
    assert "模型超时" in error_frame
    await iterator.aclose()


async def test_worker_consume_once_acks_and_handles_batch(client):
    from scripts.agent_worker import _consume_once

    queue = RedisStreamTaskQueue("", client=client, consumer="worker-1")
    broker = EventBroker()
    container = _worker_container(broker)

    await queue.enqueue(_task("task-1"))
    await queue.enqueue(_task("task-2"))
    processed = await _consume_once(
        container=container,
        task_queue=queue,
        broker=broker,
        batch=2,
        block_ms=10,
        use_stream=False,
    )
    assert processed == 2
    assert await queue.read_pending(count=10) == []
    assert (await queue.get_status("task-1")).state == "done"
    assert (await queue.get_status("task-2")).state == "done"


async def test_worker_uses_directive_from_payload(queue):
    """入队时固化的偏好指令要原样进入 worker 的 query。"""
    from scripts.agent_worker import process_task

    seen: list = []

    class _RecordingAgent:
        async def ainvoke(self, payload, config):
            seen.append(payload["messages"][0].content)
            return {"messages": [AIMessage(content="ok")]}

    broker = EventBroker()
    container = _worker_container(broker)
    container.agent = _RecordingAgent()

    await queue.enqueue(
        _task("task-directive", experiment_config={"preference_directive": "<hint>预算 200</hint>"}),
    )
    (_, task) = (await queue.read_new(count=1))[0]
    await process_task(task, container=container, broker=broker, task_queue=queue)

    assert seen and seen[0].startswith("推荐一个旅行三件套")
    assert "预算 200" in seen[0]


async def test_worker_metrics_are_written(queue):
    """worker 侧也要落 session_metrics（否则队列模式成本统计会漏）。"""
    from scripts.agent_worker import process_task

    rows: list = []

    class _MetricsStore:
        async def add(self, metrics):
            rows.append(metrics)
            return len(rows)

    broker = EventBroker()
    container = _worker_container(broker, metrics_store=_MetricsStore())

    await queue.enqueue(_task("task-metrics"))
    (_, task) = (await queue.read_new(count=1))[0]
    status = await process_task(task, container=container, broker=broker, task_queue=queue)

    assert status.state == "done"
    assert len(rows) == 1
    assert rows[0].session_id == "s1"
    assert rows[0].trace_id == "task-metrics"


async def test_worker_ack_message_id_from_stream(queue):
    """worker 消费后 ack：PEL 清空，任务不会重投。"""
    from scripts.agent_worker import process_task

    broker = EventBroker()
    container = _worker_container(broker)
    await queue.enqueue(_task("task-ack"))
    (message_id, task) = (await queue.read_new(count=1))[0]

    await process_task(task, container=container, broker=broker, task_queue=queue)
    await queue.ack(message_id)
    assert await queue.read_pending(count=10) == []
    # 已 ack 的消息不会被 claim 重投。
    assert await queue.claim_stale(min_idle_ms=1, count=10) == []


# ======================================================================
# 6. 端到端：两个实例（API 实例 + worker 实例）共用 fakeredis
# ======================================================================
async def test_end_to_end_api_enqueue_worker_consume_and_broadcast():
    """完整链路：API 入队 → worker 消费执行 → 结果广播回 API 实例的 SSE。

    用同一个 ``FakeServer`` 上的两个 client 模拟两个进程；``worker`` 侧的 EventBroker
    带背板，``api`` 侧的 EventBroker 也带背板 —— 后者才是买家的 SSE 连接所在实例。
    """
    from scripts.agent_worker import process_task

    server = fakeredis.FakeServer()
    api_backplane = RedisEventBackplane("", client=_redis(server))
    worker_backplane = RedisEventBackplane("", client=_redis(server))
    api_broker = EventBroker(backplane=api_backplane)
    worker_broker = EventBroker(backplane=worker_backplane)

    api_queue = RedisStreamTaskQueue("", client=_redis(server), consumer="api")
    worker_queue = RedisStreamTaskQueue("", client=_redis(server), consumer="worker")

    # 1) 买家在 API 实例订阅 SSE（真实前端行为）。
    iterator, reader = await _start_broker_reader(api_broker, "s-e2e")
    assert await api_broker.start() is True
    assert await worker_broker.start() is True

    # 2) POST /commerce/intents → 任务进 Redis Stream。
    task = _task("task-e2e", session_id="s-e2e")
    await api_queue.enqueue(task)
    await api_queue.set_status(TaskStatus(task_id="task-e2e", state="queued"))

    # 3) worker 实例取任务并执行（这里用假 Agent，避免真实 LLM / MySQL 依赖）。
    (message_id, consumed) = (await worker_queue.read_new(count=1))[0]
    assert consumed.task_id == "task-e2e"
    worker_container = _worker_container(worker_broker)
    status = await process_task(
        consumed,
        container=worker_container,
        broker=worker_broker,
        task_queue=worker_queue,
    )
    assert status.state == "done"
    await worker_queue.ack(message_id)

    # 4) 结果通过 Pub/Sub 广播回 API 实例，写进买家的 SSE 队列。
    #    顺序与直接模式一致：先 task.started，再 final.result。
    started_frame = await asyncio.wait_for(reader, timeout=5.0)
    assert "event: task.started" in started_frame
    assert "task-e2e" in started_frame
    frame = await _next_frame(iterator, timeout=5.0)
    assert "event: final.result" in frame
    assert "worker 回复" in frame
    assert "task-e2e" in frame

    # 5) 状态可查（task_id 与 SSE 帧里的 task_id 一致）。
    stored = await api_queue.get_status("task-e2e")
    assert stored.state == "done"
    assert stored.final_text == "worker 回复"

    await iterator.aclose()
    await api_broker.aclose()
    await worker_broker.aclose()
    await api_queue.close()
    await worker_queue.close()


# ======================================================================
# 7. worker CLI（argparse + 消费循环）
# ======================================================================
class _CliContainer:
    """CLI 冒烟用最小容器：假 Agent + 进程内 broker。"""

    def __init__(self):
        self.agent = _FakeAgent("CLI 冒烟回复")
        self.broker = EventBroker()
        self.metrics_store = None
        self.task_store = None
        self.preference_store = _FakePreferenceStore()
        self.task_queue = None

    async def close(self):
        await self.broker.stop()


def test_worker_cli_parser_defaults():
    from scripts.agent_worker import build_parser

    args = build_parser().parse_args([])
    assert args.consumer == ""
    assert args.batch == 1
    assert args.once is False
    assert args.no_stream is False
    assert args.log_level == "INFO"


async def test_worker_cli_once_consumes_preloaded_task(monkeypatch, client):
    """CLI（--once）真实走一遍：连队列 → 取任务 → 执行 → 写状态 → ack。"""
    import scripts.agent_worker as worker

    settings = SimpleNamespace(
        redis_url="redis://localhost:6379/0",
        queue_mode="redis",
        queue_stream_key="ecommerce:intents",
        queue_group="ecommerce-workers",
    )

    async def _fake_build_container(_settings, **_kwargs):
        return _CliContainer()

    class _Queue(RedisStreamTaskQueue):
        # 注入测试用的 fakeredis client（worker 内部是按 settings 自己建队列的）。
        @classmethod
        def from_settings(cls, _settings, *, client=None, consumer="", **_kwargs):
            return cls(
                "", client=fake_client, stream_key="ecommerce:intents",
                group="ecommerce-workers", consumer=consumer or "cli-smoke",
            )

    fake_client = client
    monkeypatch.setattr(worker, "load_settings", lambda: settings)
    monkeypatch.setattr(worker, "build_container", _fake_build_container)
    monkeypatch.setattr(worker, "RedisStreamTaskQueue", _Queue)

    queue = _Queue("", client=client, consumer="preload")
    await queue.enqueue(_task("cli-task-1", session_id="cli-session"))

    rc = await worker.run_worker(worker.build_parser().parse_args(["--once"]))

    assert rc == 0
    stored = await queue.get_status("cli-task-1")
    assert stored.state == "done"
    assert stored.final_text == "CLI 冒烟回复"
    # --once 走完要 ack：PEL 清空，不会被重投。
    assert await queue.read_pending(count=10) == []


async def test_consume_forever_stops_on_event(client):
    """常驻消费循环：处理完预置任务后收到停止信号即退出。"""
    import scripts.agent_worker as worker

    queue = RedisStreamTaskQueue("", client=client, consumer="worker-forever")
    broker = EventBroker()
    container = _CliContainer()
    stop_event = asyncio.Event()

    await queue.enqueue(_task("forever-1"))
    await queue.enqueue(_task("forever-2"))

    original_process = worker.process_task
    processed: list = []

    async def _process(task, **kwargs):
        processed.append(task.task_id)
        if len(processed) >= 2:
            stop_event.set()
        return await original_process(task, **kwargs)

    worker.process_task = _process
    try:
        total = await worker.consume_forever(
            container=container,
            task_queue=queue,
            broker=broker,
            stop_event=stop_event,
            batch=1,
            block_ms=10,
            claim_every=0,
            use_stream=False,
        )
    finally:
        worker.process_task = original_process

    assert total == 2
    assert processed == ["forever-1", "forever-2"]
    assert await queue.read_pending(count=10) == []
