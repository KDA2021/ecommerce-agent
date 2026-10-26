# -*- coding: utf-8 -*-
"""意图路由：``POST /commerce/intents`` + ``GET /commerce/intents/{task_id}``。

direct 模式（``QUEUE_MODE=direct``）在请求内跑 Agent；redis 模式只收单并把
任务投递到 Redis Stream，真正执行在 worker 进程。两种模式共享同一套请求体 /
响应体与完成门控逻辑。
"""
from __future__ import annotations

import hashlib
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.application.agents.main_graph import invoke_agent, invoke_agent_stream
from app.application.data_policy import DataPolicy
from app.application.evidence.completion_guard import CompletionGuard
from app.application.memory.preference_selector import render_preference_lines
from app.application.metrics.session_metrics import (
    SessionMetricsCollector,
    current_collector,
    reset_collector,
    set_collector,
)
from app.domain.queue.ports.task_queue import IntentTask, TaskStatus
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.presentation.dependencies import COMMERCE_AUTH, COMMERCE_AUTH_AND_RATE_LIMIT, get_container

logger = logging.getLogger(__name__)


class PreferenceHint(BaseModel):
    """前端偏好 chips 选择（P10-3）：随请求提交，作为本轮检索的口径提示。"""

    budget_max_major: float | None = None  # 预算上限（主单位），如 200
    budget_currency: str = "CNY"           # 预算币种，默认 CNY
    ship_to: str | None = None             # 收货国家二位码，如 US
    category: str | None = None            # 品类，如 旅行装备


class IntentRequest(BaseModel):
    """POST /commerce/intents 请求体。"""

    shopping_session_id: str  # 会话 id：同一用户多轮对话共享的 thread
    buyer_id: str             # 买家 id，用于偏好与订单归属
    query: str                # 本轮用户输入
    locale: str = "zh-CN"     # 语言/地区，默认中文
    currency: str = "CNY"     # 期望价格币种，默认人民币
    preference_hint: PreferenceHint | str | None = None  # 偏好 chips（结构化或自然语言）
    client_request_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9._:\-]*$")


class IntentResponse(BaseModel):
    """意图接口响应体（direct 模式）。"""

    shopping_session_id: str  # 回显会话 id
    reply: str                # 经过完成门控后的最终回复
    task_id: str = ""         # P11-4 队列模式回填；direct 模式为空
    status: str = ""          # queued / done ...；direct 模式为空
    trace_id: str = ""
    client_request_id: str = ""


class TaskStatusResponse(BaseModel):
    """GET /commerce/intents/{task_id} 响应体（P11-4 队列模式）。"""

    task_id: str                 # 任务 id
    state: str                   # queued / running / done / failed
    found: bool                  # 状态是否存在（TTL 过期后为 False）
    final_text: str = ""         # done 时的最终回复
    error: str = ""              # failed 时的错误摘要
    queue_position: int = 0      # 队列位置（预留；当前实现为 0）


async def _thread_has_history(agent, session_id: str) -> bool:
    """首轮判定：checkpointer 中该 thread 已有消息即视为“有历史”。"""
    aget_state = getattr(agent, "aget_state", None)
    if aget_state is None:
        return False
    try:
        snapshot = await aget_state({"configurable": {"thread_id": session_id}})
    except Exception:  # noqa: BLE001
        return False
    values = getattr(snapshot, "values", None) or {}
    return bool(values.get("messages"))


async def _preference_scope(container, buyer_id: str) -> str:
    """按买家偏好渲染文本计算 sha256 前 16 位作用域指纹。

    Args:
        container: 装配容器。
        buyer_id: 买家 id。

    Returns:
        指纹串；无偏好或读取失败时返回空串。
    """
    try:
        prefs = await container.preference_store.list_by_buyer(buyer_id)
    except Exception:
        return ""
    if not prefs:
        return ""
    return hashlib.sha256(render_preference_lines(prefs).encode()).hexdigest()[:16]


def render_preference_directive(hint: "PreferenceHint | str | None") -> str:
    """把前端偏好 chips 渲染成注入 Agent 的一行指令文本。

    只做"把用户显式选择的口径转达给 Agent"，不替用户臆测：没填的槽位不出现。

    Args:
        hint: 结构化偏好 chips 或自然语言偏好提示；为空时返回空串。

    Returns:
        形如 ``<frontend-preferences>预算上限 200 CNY；收货国家 US</frontend-preferences>``
        的文本；无有效槽位时返回空串。
    """
    if not hint:
        return ""
    if isinstance(hint, str):
        text = hint.strip()
        return f"<frontend-preferences>\n{text}\n</frontend-preferences>" if text else ""

    parts: list[str] = []
    if hint.budget_max_major is not None:
        # 200.0 与 200 对买家是同一个意思：整数就按整数渲染，别让 prompt 出现 ".0"。
        budget = hint.budget_max_major
        budget_text = str(int(budget)) if float(budget).is_integer() else str(budget)
        parts.append(f"预算上限 {budget_text} {hint.budget_currency}")
    if hint.ship_to:
        parts.append(f"收货国家 {hint.ship_to}")
    if hint.category:
        parts.append(f"品类 {hint.category}")
    if not parts:
        return ""
    return (
        "<frontend-preferences>\n买家在前端选择的检索口径（请作为 product_search 的"
        "price_max_major / ship_to / category 槽位传入）："
        + "；".join(parts)
        + "\n</frontend-preferences>"
    )


async def _collect_verified_order_ids(task_store, trace_id: str) -> list[str]:
    """兼容不同 TaskStateStore 测试替身，收集当前 trace 的已验证订单号。

    Args:
        task_store: 任务状态存储。
        trace_id: 当前请求 trace id。

    Returns:
        已验证订单号列表。
    """
    # 优先用 store 的便捷方法；测试替身没有时，退化为遍历记录自行筛选。
    getter = getattr(task_store, "verified_order_ids", None)
    if callable(getter):
        return list(await getter(trace_id))

    lister = getattr(task_store, "list_by_trace", None)
    if not callable(lister):
        return []
    verified: list[str] = []
    for record in await lister(trace_id) or []:
        status = getattr(record, "status", None)
        if getattr(status, "value", status) != "VERIFIED":
            continue
        order_id = getattr(record, "verification_order_id", None)
        if callable(order_id):
            order_id = order_id()
        if order_id and str(order_id) not in verified:
            verified.append(str(order_id))
    return verified


async def _guard_completion(container, trace_id: str, reply: str) -> str:
    """用当前 trace 的 VERIFIED 订单证据门控最终回复。

    Args:
        container: 装配容器（测试替身可以没有 task_store）。
        trace_id: 当前请求 trace id。
        reply: Agent / 缓存原始回复。

    Returns:
        原回复或安全话术。
    """
    task_store = getattr(container, "task_store", None)
    verified_order_ids: list[str] = []
    if task_store is not None and trace_id:
        try:
            verified_order_ids = await _collect_verified_order_ids(task_store, trace_id)
        except Exception:  # noqa: BLE001 - 证据读取失败按“未验证”处理，门控 fail closed
            verified_order_ids = []
    policy = getattr(container, "data_policy", None) or DataPolicy()
    return policy.sanitize_output(CompletionGuard.guard(reply, verified_order_ids))


async def _record_session_metrics(
    container,
    collector: SessionMetricsCollector | None,
    *,
    session_id: str,
    trace_id: str,
    query: str,
    reply: str = "",
) -> None:
    """把本轮成本 / 用量写进 session_metrics。

    指标是旁路能力：store 缺失、写库失败都只记 warning，绝不影响主流程。

    Args:
        container: 装配容器（测试替身可以没有 metrics_store）。
        collector: 本轮采集器；为 None 时跳过。
        session_id: 会话 id。
        trace_id: 请求级 trace id。
        query: 本轮用户问题（无流式 token 时用于估算）。
        reply: 最终回复（无流式 token 时用于估算）。
    """
    store = getattr(container, "metrics_store", None)
    if store is None or collector is None:
        return
    try:
        metrics = collector.build(
            session_id=session_id,
            trace_id=trace_id,
            turns=1,
            fallback_chars=len(query or "") + len(reply or ""),
        )
        await store.add(metrics)
    except Exception as exc:  # noqa: BLE001 - 指标写失败不能影响对话主流程
        logger.warning("session_metrics 写入失败 session=%s: %s", session_id, exc)


def _new_intent_snapshot(body: IntentRequest) -> ShoppingContextSnapshot:
    """生成本轮会话快照（含新 trace_id）。

    Args:
        body: 意图请求体。

    Returns:
        ShoppingContextSnapshot。
    """
    return ShoppingContextSnapshot(
        shopping_session_id=body.shopping_session_id,
        buyer_id=body.buyer_id,
        locale=body.locale,
        currency=body.currency,
        trace_id=uuid4().hex,
        client_request_id=body.client_request_id,
    )


def _queued_intent_response(task_id: str, session_id: str, client_request_id: str = "") -> IntentResponse:
    """构造队列模式（``QUEUE_MODE=redis``）的接受响应。"""
    return IntentResponse(
        shopping_session_id=session_id,
        reply="",
        task_id=task_id,
        status="queued",
        trace_id=task_id,
        client_request_id=client_request_id,
    )


def _queue_mode_enabled(container) -> bool:
    """判断当前容器是否处于队列模式（``QUEUE_MODE=redis`` 装配出的容器）。

    约定：带 ``task_queue`` 属性的容器就是队列模式（bootstrap 在 direct 模式下
    显式把 ``task_queue`` 置 None）。不依赖 ``settings``，测试替身只要不声明
    ``task_queue`` 就自动走 direct 路径。

    Args:
        container: 装配容器或测试替身。

    Returns:
        ``True`` 表示应当走队列路径。
    """
    return getattr(container, "task_queue", None) is not None


async def _enqueue_intent(container, body: IntentRequest, snapshot: ShoppingContextSnapshot) -> IntentResponse:
    """把本轮意图投递到 Redis Stream，并立刻返回 task_id。

    客户端拿到 ``task_id`` 后有两种方式收敛：订阅 ``/commerce/events`` 等
    ``final.result``，或轮询 ``/commerce/intents/{task_id}``。

    入队时就把「本轮偏好指令」渲染进 payload：worker 进程没有前端的原始 chips，
    必须在收单处固化，避免 worker 侧凭偏好记忆二次猜测用户口径。

    Args:
        container: 装配容器（必须有可用的 ``task_queue``）。
        body: 意图请求体。
        snapshot: 本轮会话快照（含 trace_id）。

    Returns:
        IntentResponse（``status=queued`` + ``task_id``）。

    Raises:
        HTTPException: 队列未就绪 / 入队失败返回 503。
    """
    task_queue = getattr(container, "task_queue", None)
    if task_queue is None or not getattr(task_queue, "enabled", False):
        # 队列模式但队列不可用：必须 fail loud，不能悄悄退回请求内直跑
        # （否则客户端会以为任务已入队、一直在等 final.result）。
        raise HTTPException(status_code=503, detail="任务队列未就绪（请检查 QUEUE_MODE 与 REDIS_URL）")

    payload_config = dict(getattr(container, "experiment_config", None) or {})
    directive = render_preference_directive(body.preference_hint)
    if directive:
        payload_config["preference_directive"] = directive
    task = IntentTask(
        # 用 trace_id 当任务号：客户端拿到的 trace_id 与 SSE / trace 行完全一致，
        # 便于把「一次请求」在队列、日志、事件三处对齐。
        task_id=snapshot.trace_id or uuid4().hex,
        shopping_session_id=body.shopping_session_id,
        buyer_id=body.buyer_id,
        locale=body.locale,
        currency=body.currency,
        raw_query=body.query,
        experiment_config=payload_config,
        client_request_id=body.client_request_id,
    )
    try:
        message_id = await task_queue.enqueue(task)
    except Exception as err:  # noqa: BLE001 - 入队失败必须让客户端知道，不能假装成功
        raise HTTPException(status_code=503, detail=f"任务入队失败：{err}") from err
    logger.info(
        "意图任务已入队 task=%s session=%s stream_id=%s",
        task.task_id,
        body.shopping_session_id,
        message_id,
    )
    # 队列模式下请求线程只负责收单：真正的 running / done 状态由 worker 回写。
    await task_queue.set_status(
        TaskStatus(task_id=task.task_id, state="queued", queue_position=0),
    )
    return _queued_intent_response(task.task_id, body.shopping_session_id, body.client_request_id)


async def _run_intent_direct(container, body: IntentRequest) -> IntentResponse:
    """direct 模式：查缓存 → 流式跑 Agent → 回写缓存（与 P11-3 行为完全一致）。

    Args:
        container: 装配容器。
        body: 请求体。

    Returns:
        IntentResponse。

    Raises:
        HTTPException: Agent 执行失败返回 500。
    """
    snapshot = _new_intent_snapshot(body)
    token = ShoppingContext.set(snapshot)
    metrics_token = set_collector(SessionMetricsCollector())
    collector = current_collector()
    try:
        has_history = await _thread_has_history(container.agent, body.shopping_session_id)
        # P10-3：前端偏好 chips 只作为本轮"检索口径提示"注入，不参与缓存 key。
        directive = render_preference_directive(body.preference_hint)
        agent_query = f"{body.query}\n\n{directive}" if directive else body.query
        scope = await _preference_scope(container, body.buyer_id)
        hit = None
        # 仅首轮查语义缓存：多轮回复依赖上下文，直接复用旧答案会“串味”。
        if not has_history:
            hit = await container.semantic_cache.lookup(
                body.buyer_id, body.query, has_history=False, scope=scope,
            )
        if hit is not None:
            # 缓存命中可跳过 Agent，但写操作门控仍要执行。
            guarded_reply = await _guard_completion(container, snapshot.trace_id, hit.reply)
            container.broker.publish(
                body.shopping_session_id,
                "cache.hit",
                {
                    "query": body.query,
                    "matched_query": hit.matched_query,
                    "similarity": hit.similarity,
                },
            )
            container.broker.publish(
                body.shopping_session_id, "final.result", {"reply": guarded_reply},
            )
            # 缓存命中同样算一轮成本（只是 LLM 调用为 0）。
            await _record_session_metrics(
                container,
                collector,
                session_id=body.shopping_session_id,
                trace_id=snapshot.trace_id,
                query=body.query,
                reply=guarded_reply,
            )
            return IntentResponse(
                shopping_session_id=body.shopping_session_id,
                reply=guarded_reply,
                trace_id=snapshot.trace_id,
                client_request_id=body.client_request_id,
            )

        if container.broker is not None:
            # 有 broker 才走流式：token.delta 才能实时进 SSE 队列。
            reply = await invoke_agent_stream(
                container.agent, body.shopping_session_id, agent_query, container.broker,
                data_policy=getattr(container, "data_policy", None),
                output_guard=lambda text: _guard_completion(container, snapshot.trace_id, text),
            )
        else:
            reply = await invoke_agent(
                container.agent, body.shopping_session_id, agent_query,
                data_policy=getattr(container, "data_policy", None),
            )
    except Exception as err:  # noqa: BLE001
        policy = getattr(container, "data_policy", None) or DataPolicy()
        safe_error = policy.sanitize_output(str(err))
        if container.broker is not None:
            container.broker.publish(body.shopping_session_id, "error", {"message": safe_error})
        # 失败请求也要留一行，避免成本统计漏掉真实发生过的 LLM 调用。
        await _record_session_metrics(
            container,
            collector,
            session_id=body.shopping_session_id,
            trace_id=snapshot.trace_id,
            query=body.query,
        )
        raise HTTPException(status_code=500, detail=safe_error) from None
    finally:
        reset_collector(metrics_token)
        ShoppingContext.reset(token)

    reply = await _guard_completion(container, snapshot.trace_id, reply)
    if container.broker is not None:
        container.broker.publish(body.shopping_session_id, "final.result", {
            "reply": reply, "trace_id": snapshot.trace_id,
            "client_request_id": body.client_request_id,
        })
    # 仅首轮写缓存，避免把依赖上下文的回复固化成通用答案。
    if not has_history:
        await container.semantic_cache.remember(
            body.buyer_id, body.query, reply, has_history=False, scope=scope,
        )
    await _record_session_metrics(
        container,
        collector,
        session_id=body.shopping_session_id,
        trace_id=snapshot.trace_id,
        query=body.query,
        reply=reply,
    )
    return IntentResponse(shopping_session_id=body.shopping_session_id, reply=reply,
                          trace_id=snapshot.trace_id, client_request_id=body.client_request_id)


def build_intents_router() -> APIRouter:
    """构造意图路由（direct / 队列两种模式 + 任务状态查询）。

    Returns:
        APIRouter（``/commerce/intents``、``/commerce/intents/{task_id}``）。
    """
    router = APIRouter()

    @router.post(
        "/commerce/intents",
        response_model=IntentResponse,
        dependencies=COMMERCE_AUTH_AND_RATE_LIMIT,
    )
    async def submit_intent(body: IntentRequest, request: Request) -> IntentResponse:
        """一轮对话入口：direct 直接跑 Agent；redis 模式只入队并返回 task_id。

        Args:
            body: 请求体。
            request: FastAPI 请求（取容器）。

        Returns:
            IntentResponse。direct 模式带 ``reply``；redis 模式带 ``task_id`` + ``status=queued``。

        Raises:
            HTTPException: redis 模式入队失败返回 503。
        """
        from app.presentation.auth import principal
        from app.presentation.ownership import authorize_session

        user = principal(request)
        if user is not None:
            if body.buyer_id != user.buyer_id:
                raise HTTPException(status_code=403, detail="买家编号与登录身份不一致")
            body = body.model_copy(update={"buyer_id": user.buyer_id})
        await authorize_session(request, body.shopping_session_id, claim=True)
        container = get_container(request)
        # P11-4：QUEUE_MODE=redis 时请求线程只收单，真实执行在 worker 进程。
        if _queue_mode_enabled(container):
            return await _enqueue_intent(container, body, _new_intent_snapshot(body))
        return await _run_intent_direct(container, body)

    # P11-4：队列模式（QUEUE_MODE=redis）的任务状态查询。
    # 与 SSE 二选一即可：轮询更适合没有长连接的客户端 / 网关。
    @router.get("/commerce/intents/{task_id}", dependencies=COMMERCE_AUTH)
    async def get_intent_task(task_id: str, request: Request) -> TaskStatusResponse:
        """查询队列任务状态。

        Args:
            task_id: ``POST /commerce/intents`` 返回的 task_id。
            request: FastAPI 请求（取容器）。

        Returns:
            TaskStatusResponse；状态不存在时 ``found=False`` + ``state=unknown``。
        """
        container = get_container(request)
        task_queue = getattr(container, "task_queue", None) if container is not None else None
        if task_queue is None or not getattr(task_queue, "enabled", False):
            raise HTTPException(status_code=503, detail="任务队列未启用（QUEUE_MODE=direct）")
        status = await task_queue.get_status(task_id)
        from app.presentation.auth import principal

        user = principal(request)
        if user is not None:
            getter = getattr(task_queue, "get_task_owner", None)
            if getter is None:
                raise HTTPException(status_code=503, detail="任务归属存储未就绪")
            if await getter(task_id) != user.buyer_id:
                raise HTTPException(status_code=404, detail="任务不存在")
        if status is None:
            return TaskStatusResponse(task_id=task_id, state="unknown", found=False)
        return TaskStatusResponse(
            task_id=status.task_id,
            state=status.state,
            found=True,
            final_text=status.final_text,
            error=status.error,
            queue_position=status.queue_position,
        )

    return router
