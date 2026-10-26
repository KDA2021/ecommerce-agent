# -*- coding: utf-8 -*-
"""P11-4 多 worker 消费者：从 Redis Stream 取意图任务并跑 Agent。

用法::

    # .env: QUEUE_MODE=redis / REDIS_URL=redis://localhost:6379/0
    uv run --no-sync python scripts/agent_worker.py
    uv run --no-sync python scripts/agent_worker.py --consumer worker-1 --batch 4
    uv run --no-sync python scripts/agent_worker.py --once     # 跑一个批次后退出（CI / 冒烟）

一次任务的生命周期：

1. ``XREADGROUP`` 取消息（进入 PEL，未 ack）；
2. 写 ``ecommerce:task:{task_id}`` = running；
3. 绑定 ShoppingContext 后调用现有 ``invoke_agent``（流式时用 ``invoke_agent_stream``，
   把 token.delta 实时发到背板）；
4. 发布 ``final.result``（以及失败时的 ``error``）到 ``ecommerce:events:{session_id}``；
5. 写 done / failed 状态，最后 ``XACK``。

失败策略：业务异常写 failed 状态 + 发 error 事件后 **ack**（否则同一条毒消息会被
无限重投）；Redis 连接级异常则不 ack，交给 ``XAUTOCLAIM`` 重投。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path
from typing import Any

# 允许 `python scripts/agent_worker.py` 直接运行（sys.path[0] 是 scripts/ 而不是项目根）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application.agents.main_graph import invoke_agent, invoke_agent_stream  # noqa: E402
from app.application.evidence.completion_guard import CompletionGuard  # noqa: E402
from app.application.metrics.session_metrics import (  # noqa: E402
    SessionMetricsCollector,
    current_collector,
    reset_collector,
    set_collector,
)
from app.bootstrap import Container, build_container  # noqa: E402
from app.domain.queue.ports.task_queue import IntentTask, TaskStatus  # noqa: E402
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot  # noqa: E402
from app.infrastructure.logging_config import (  # noqa: E402
    bind_request_context,
    configure_logging,
)
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue  # noqa: E402
from app.infrastructure.settings import load_settings  # noqa: E402
from app.infrastructure.tracing import traced_span  # noqa: E402

logger = logging.getLogger("agent_worker")

#: XREADGROUP 单次阻塞毫秒数；同时决定 Ctrl-C 的响应粒度。
DEFAULT_BLOCK_MS = 2000
#: 每次 claim 巡检前等待的消费轮数。
DEFAULT_CLAIM_EVERY = 15


def default_consumer_name() -> str:
    """默认消费者名 ``host-pid``，保证多进程 worker 不重名。

    Returns:
        消费者名字符串。
    """
    try:
        host = socket.gethostname() or "host"
    except Exception:  # pragma: no cover
        host = "host"
    import os

    return f"{host}-{os.getpid()}"


# ----------------------------------------------------------------------
# 单任务处理
# ----------------------------------------------------------------------
async def process_task(
    task: IntentTask,
    *,
    container: Container,
    broker: Any,
    task_queue: RedisStreamTaskQueue,
    use_stream: bool = True,
) -> TaskStatus:
    """执行一个意图任务，并把 ``request_id`` / ``trace_id`` 注入日志上下文。

    worker 没有 HTTP 请求，用 ``task_id`` 同时充当 request_id / trace_id：
    一次任务期间（含重试 / 失败）打出的日志都能用同一个 ID 串起来。

    Args:
        task: 队列任务。
        container: 装配容器。
        broker: 事件总线。
        task_queue: 任务队列。
        use_stream: 是否优先走流式 Agent。

    Returns:
        最终 TaskStatus（done / failed）。
    """
    with bind_request_context(request_id=task.task_id, trace_id=task.task_id):
        return await _process_task(
            task,
            container=container,
            broker=broker,
            task_queue=task_queue,
            use_stream=use_stream,
        )


async def _process_task(
    task: IntentTask,
    *,
    container: Container,
    broker: Any,
    task_queue: RedisStreamTaskQueue,
    use_stream: bool = True,
) -> TaskStatus:
    """执行一个意图任务：绑定上下文 → 跑 Agent → 发事件 → 写状态。

    Args:
        task: 队列任务。
        container: 装配容器。
        broker: 事件总线（带背板时事件会广播到其他实例）。
        task_queue: 任务队列（写状态用）。
        use_stream: 是否优先走流式 Agent。

    Returns:
        最终 TaskStatus（done / failed）。
    """
    trace_id = task.task_id
    session_id = task.shopping_session_id
    snapshot = ShoppingContextSnapshot(
        shopping_session_id=session_id,
        buyer_id=task.buyer_id,
        locale=task.locale,
        currency=task.currency,
        trace_id=trace_id,
        task_id=task.task_id,
        client_request_id=task.client_request_id,
    )
    context_token = ShoppingContext.set(snapshot)
    metrics_token = set_collector(SessionMetricsCollector())
    collector = current_collector()

    await task_queue.set_status(TaskStatus(task_id=trace_id, state="running"))
    _publish(broker, session_id, "task.started", {"task_id": trace_id})
    logger.info("任务开始 task=%s session=%s", trace_id, session_id)

    try:
        query = await _build_agent_query(container, task)
        tracer = getattr(broker, "trace_logger", None)
        async with traced_span(
            tracer,
            "worker.task",
            {"task_id": trace_id, "session_id": session_id, "queue": task_queue.stream_key},
        ):
            if use_stream and broker is not None:
                reply = await invoke_agent_stream(
                    container.agent,
                    session_id,
                    query,
                    broker,
                    data_policy=getattr(container, "data_policy", None),
                    output_guard=lambda text: _guard_completion(container, trace_id, text),
                )
            else:
                reply = await invoke_agent(
                    container.agent,
                    session_id,
                    query,
                    data_policy=getattr(container, "data_policy", None),
                )
        reply = await _guard_completion(container, trace_id, reply)
    except Exception as err:  # noqa: BLE001 - 单条任务失败不能让 worker 退出
        from app.application.data_policy import DataPolicy

        policy = getattr(container, "data_policy", None) or DataPolicy()
        safe_error = policy.sanitize_output(str(err))
        logger.error("任务失败 task=%s error=%s", trace_id, safe_error)
        _publish(broker, session_id, "error", {"task_id": trace_id, "message": safe_error})
        status = TaskStatus(task_id=trace_id, state="failed", error=safe_error)
    else:
        _publish(broker, session_id, "final.result", {"task_id": trace_id, "reply": reply})
        status = TaskStatus(task_id=trace_id, state="done", final_text=reply)
    finally:
        try:
            # 失败请求也要留一行指标，避免漏掉真实发生过的 LLM 调用。
            await _record_metrics(
                container,
                collector,
                session_id=session_id,
                trace_id=trace_id,
                task=task,
                reply=status.final_text,
                error=status.error,
            )
        finally:
            reset_collector(metrics_token)
            ShoppingContext.reset(context_token)

    await task_queue.set_status(status)
    logger.info("任务结束 task=%s state=%s", trace_id, status.state)
    return status


async def _build_agent_query(container: Container, task: IntentTask) -> str:
    """拼出 worker 侧最终 query（偏好口径在入队时已由 server 渲染好）。

    Args:
        container: 装配容器（保留参数以便将来补 worker 侧上下文）。
        task: 队列任务。

    Returns:
        带偏好指令的最终 query（无偏好时等于原始 query）。
    """
    directive = str((task.experiment_config or {}).get("preference_directive") or "").strip()
    if directive:
        return f"{task.raw_query}\n\n{directive}"
    return task.raw_query


async def _guard_completion(container: Container, trace_id: str, reply: str) -> str:
    """用 VERIFIED 订单证据门控最终回复（与 server 同口径）。"""
    task_store = getattr(container, "task_store", None)
    verified: list[str] = []
    if task_store is not None and trace_id:
        try:
            getter = getattr(task_store, "verified_order_ids", None)
            if callable(getter):
                verified = list(await getter(trace_id))
        except Exception:  # noqa: BLE001 - 证据读取失败 fail closed
            verified = []
    from app.application.data_policy import DataPolicy

    policy = getattr(container, "data_policy", None) or DataPolicy()
    return policy.sanitize_output(CompletionGuard.guard(reply, verified))


async def _record_metrics(
    container: Container,
    collector,
    *,
    session_id: str,
    trace_id: str,
    task: IntentTask,
    reply: str = "",
    error: str = "",
) -> None:
    """把 worker 侧这轮成本 / 用量写进 session_metrics（旁路，失败只记 warning）。"""
    store = getattr(container, "metrics_store", None)
    if store is None or collector is None:
        return
    try:
        metrics = collector.build(
            session_id=session_id,
            trace_id=trace_id,
            turns=1,
            fallback_chars=len(task.raw_query or "") + len(reply or "") + len(error or ""),
        )
        await store.add(metrics)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_metrics 写入失败 session=%s: %s", session_id, exc)


def _publish(broker: Any, session_id: str, event_type: str, payload: dict) -> None:
    """best-effort 发布事件（broker 缺失或异常都不影响任务状态）。"""
    if broker is None:
        return
    try:
        broker.publish(session_id, event_type, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("事件发布失败 %s session=%s err=%s", event_type, session_id, exc)


# ----------------------------------------------------------------------
# 消费循环
# ----------------------------------------------------------------------
async def consume_forever(
    *,
    container: Container,
    task_queue: RedisStreamTaskQueue,
    broker: Any,
    stop_event: asyncio.Event,
    batch: int = 1,
    block_ms: int = DEFAULT_BLOCK_MS,
    claim_every: int = DEFAULT_CLAIM_EVERY,
    use_stream: bool = True,
) -> int:
    """长驻消费循环，直到 ``stop_event`` 被置位。

    Args:
        container: 装配容器。
        task_queue: Redis Stream 队列。
        broker: 事件总线。
        stop_event: 停止信号（Ctrl-C / SIGTERM 置位）。
        batch: 每轮最多取几条。
        block_ms: 每轮 XREADGROUP 阻塞毫秒。
        claim_every: 每消费多少轮做一次 pending 巡检。
        use_stream: 是否走流式 Agent。

    Returns:
        处理过的任务总数。
    """
    processed = 0
    rounds = 0
    while not stop_event.is_set():
        rounds += 1
        if claim_every and rounds % claim_every == 0:
            await _reclaim_stale(task_queue, broker, container, use_stream=use_stream)
        messages = await task_queue.read_new(count=batch, block_ms=block_ms)
        for message_id, task in messages:
            await process_task(
                task,
                container=container,
                broker=broker,
                task_queue=task_queue,
                use_stream=use_stream,
            )
            # 处理完（含业务失败）就 ack：毒消息不能永远占着 PEL。
            acked = await task_queue.ack(message_id)
            if not acked:
                logger.warning("任务 ack 失败 message=%s task=%s", message_id, task.task_id)
            processed += 1
    return processed


async def _reclaim_stale(
    task_queue: RedisStreamTaskQueue,
    broker: Any,
    container: Container,
    *,
    use_stream: bool,
) -> int:
    """把超时未 ack 的消息重投并直接在本进程处理。

    重投语义是 at-least-once：同一条任务可能被执行两次，因此下游写操作必须幂等
    （见 ``app/domain/queue/ports/task_queue.py`` 的端口约定）。

    Args:
        task_queue: 队列。
        broker: 事件总线。
        container: 装配容器。
        use_stream: 是否走流式 Agent。

    Returns:
        重投并处理的任务数。
    """
    try:
        stale = await task_queue.claim_stale(min_idle_ms=task_queue_claim_idle_ms(), count=10)
    except Exception as exc:  # noqa: BLE001 - 巡检失败不影响正常消费
        logger.warning("pending 巡检失败：%s", exc)
        return 0
    handled = 0
    for message_id, task, deliveries in stale:
        logger.warning(
            "重投超时任务 message=%s task=%s deliveries=%s",
            message_id,
            task.task_id,
            deliveries,
        )
        await process_task(
            task,
            container=container,
            broker=broker,
            task_queue=task_queue,
            use_stream=use_stream,
        )
        await task_queue.ack(message_id)
        handled += 1
    return handled


def task_queue_claim_idle_ms() -> int:
    """pending 重投的空闲阈值（秒级环境变量，缺省 60s）。"""
    import os

    try:
        return max(1000, int(float(os.getenv("QUEUE_CLAIM_IDLE_SECONDS", "60")) * 1000))
    except ValueError:
        return 60_000


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
async def run_worker(args: argparse.Namespace) -> int:
    """装配容器 / 连接背板 / 跑消费循环。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码（0 = 正常退出）。
    """
    settings = load_settings()
    queue_mode = (getattr(settings, "queue_mode", "direct") or "direct").strip().lower()
    if queue_mode != "redis":
        logger.warning(
            "QUEUE_MODE=%s 不是 redis：worker 仍会启动并消费 %s，但服务端可能并不入队",
            queue_mode,
            getattr(settings, "queue_stream_key", "ecommerce:intents"),
        )
    if not (getattr(settings, "redis_url", "") or ""):
        logger.error("REDIS_URL 为空：worker 无法消费，退出")
        return 2

    container = await build_container(settings)
    consumer = args.consumer or default_consumer_name()
    task_queue = RedisStreamTaskQueue.from_settings(settings, consumer=consumer)
    broker = container.broker

    try:
        if not await task_queue.ensure_ready():
            logger.error("Redis Stream / 消费者组未就绪，worker 退出")
            return 2
        # 订阅背板：worker 自己也要收跨实例事件（例如别的 worker 发的 final.result 不影响它，
        # 但保持一致的 fanout 语义，便于单实例调试时也能看到广播链路）。
        await broker.start()

        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        logger.info(
            "worker 启动 consumer=%s stream=%s group=%s batch=%s",
            consumer,
            task_queue.stream_key,
            task_queue.group,
            args.batch,
        )
        if args.once:
            processed = await _consume_once(
                container=container,
                task_queue=task_queue,
                broker=broker,
                batch=args.batch,
                block_ms=args.block_ms,
                use_stream=not args.no_stream,
            )
        else:
            processed = await consume_forever(
                container=container,
                task_queue=task_queue,
                broker=broker,
                stop_event=stop_event,
                batch=args.batch,
                block_ms=args.block_ms,
                use_stream=not args.no_stream,
            )
        logger.info("worker 退出，处理任务数=%s", processed)
        return 0
    finally:
        await task_queue.close()
        await container.close()


async def _consume_once(
    *,
    container: Container,
    task_queue: RedisStreamTaskQueue,
    broker: Any,
    batch: int,
    block_ms: int,
    use_stream: bool,
) -> int:
    """消费一个批次（--once / 冒烟测试用）；含一次 pending 巡检。"""
    await _reclaim_stale(task_queue, broker, container, use_stream=use_stream)
    messages = await task_queue.read_new(count=batch, block_ms=block_ms)
    for message_id, task in messages:
        await process_task(
            task,
            container=container,
            broker=broker,
            task_queue=task_queue,
            use_stream=use_stream,
        )
        await task_queue.ack(message_id)
    return len(messages)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """把 SIGINT / SIGTERM 转成优雅停止信号。

    Args:
        stop_event: 停止事件。
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows 兼容
            pass


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    Returns:
        ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="P11-4 Redis Stream 意图任务 worker")
    parser.add_argument("--consumer", default="", help="消费者名（缺省 host-pid）")
    parser.add_argument("--batch", type=int, default=1, help="每轮最多取几条任务")
    parser.add_argument(
        "--block-ms", type=int, default=DEFAULT_BLOCK_MS, help="XREADGROUP 阻塞毫秒"
    )
    parser.add_argument("--once", action="store_true", help="只消费一个批次后退出")
    parser.add_argument("--no-stream", action="store_true", help="不用流式 Agent（调试用）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 可选参数列表（默认取 sys.argv）。

    Returns:
        进程退出码。
    """
    args = build_parser().parse_args(argv)
    # P15-3：与 server 共用同一套结构化日志（LOG_FORMAT=text 退回可读文本）；
    # ``--log-level`` 仍优先于环境变量 LOG_LEVEL，非法值回落 INFO。
    configure_logging(level=str(args.log_level).upper())
    try:
        return asyncio.run(run_worker(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        logger.info("worker 被 Ctrl-C 中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
