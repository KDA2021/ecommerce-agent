# -*- coding: utf-8 -*-
"""planner / trade 条件边与派发登记辅助函数。

从 ``main_graph.py`` 抽出，依赖（broker / task_store / max_steps）全部通过
参数传入，便于单独测试与复用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from langgraph.types import Send

from app.application.agents.state import MainState
from app.application.agents.subagents.base import (
    SUBAGENT_UNAVAILABLE,
    PlannerDecision,
    subtask_task_id,
)
from app.application.agents.task_context import task_context
from app.application.evidence.task_state import TaskRecord, TaskStatus
from app.infrastructure.context import ShoppingContext

logger = logging.getLogger(__name__)


def _publish_dispatch(broker, sends: list[Send]) -> None:
    """把 Send 列表逐条发布为 ``agent.dispatch`` 事件（broker 为空则跳过）。

    Args:
        broker: 事件总线，可为 None。
        sends: LangGraph Send 列表。
    """
    if broker is None:
        return
    for send in sends:
        subtask = send.arg.get("subtask", {})
        broker.publish(
            ShoppingContext.current_session_id(),
            "agent.dispatch",
            {
                "agent": send.node,
                "instruction": subtask.get("instruction", ""),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )


async def _register_subtask(task_store, task_id: str, action: str) -> None:
    """派发前登记 PLANNED 任务记录。

    已存在的 task 直接保留（避免父图重放时把更高 generation 的租约降级）；
    登记失败只记日志、不阻断派发，节点租约阶段会以 ``[subagent unavailable]``
    如实暴露。

    Args:
        task_store: 任务状态存储，可为 None。
        task_id: 任务 id。
        action: 动作名（通常为子 Agent 名）。
    """
    if task_store is None:
        return
    try:
        if await task_store.get(task_id) is not None:
            return
        await task_store.put(
            TaskRecord(
                task_id=task_id,
                trace_id=ShoppingContext.current_trace_id(),
                action=action,
                generation=1,
                status=TaskStatus.PLANNED,
                idempotency_key=task_id[:128],
            )
        )
    except Exception as exc:  # noqa: BLE001 - 登记失败不阻断派发
        logger.warning("子任务登记失败 task_id=%s: %s", task_id, exc)


def _status_text(status) -> str:
    """把 TaskStatus / 字符串统一成可比较的字符串值。

    Args:
        status: TaskStatus 枚举或字符串。

    Returns:
        状态字符串；未知时返回空串。
    """
    return str(getattr(status, "value", status) or "")


def _subagent_marker_in(messages) -> bool:
    """判断一组消息里是否出现子 Agent 不可用标记。

    Args:
        messages: 消息列表。

    Returns:
        出现 ``[subagent unavailable]`` 时返回 True。
    """
    for message in messages or []:
        content = getattr(message, "content", "") or ""
        if SUBAGENT_UNAVAILABLE in str(content):
            return True
    return False


def _last_message_content(messages) -> str:
    """取最后一条非空消息的文本内容。

    Args:
        messages: 消息列表。

    Returns:
        最后一条非空消息的文本；无消息时返回空串。
    """
    for message in reversed(list(messages or [])):
        content = getattr(message, "content", "") or ""
        if content:
            return str(content)
    return ""


async def _inspect_trade_result(
    task_store,
    task_id: str,
    generation: int,
    messages,
) -> dict | None:
    """判定刚结束的 trade 子任务能否让串行链继续。

    有 ``task_store`` 时以存储状态为准（只有 VERIFIED 算成功；记录缺失 / 查询异常
    一律 fail-closed）；没有存储时回退到消息里的 ``[subagent unavailable]`` 标记。

    Args:
        task_store: 任务状态存储，可为 None。
        task_id: 本步 trade 子任务 id。
        generation: 本步代次。
        messages: 本步 trade 节点新增的消息列表。

    Returns:
        成功返回 None；失败返回一条 ``task_failures`` 记录 dict。
    """
    if task_store is not None and task_id:
        try:
            record = await task_store.get(task_id)
        except Exception as exc:  # noqa: BLE001 - 查询失败按未知处理，宁可停链
            return {
                "task_id": task_id,
                "generation": generation,
                "status": "UNKNOWN",
                "reason": f"任务状态查询失败：{exc}",
            }
        if record is None:
            return {
                "task_id": task_id,
                "generation": generation,
                "status": "UNKNOWN",
                "reason": "任务记录缺失，无法确认 VERIFIED",
            }
        status = _status_text(getattr(record, "status", None))
        if status != TaskStatus.VERIFIED.value:
            return {
                "task_id": task_id,
                "generation": int(getattr(record, "generation", generation) or generation),
                "status": status or "UNKNOWN",
                "reason": "上一 trade 任务未 VERIFIED",
            }
        return None
    if _subagent_marker_in(messages):
        return {
            "task_id": task_id,
            "generation": generation,
            "status": "FAILED",
            "reason": "子 Agent 返回不可用标记",
        }
    return None


async def route_after_planner(state: MainState, broker=None, task_store=None):
    """planner 条件边：按 ``decision.route`` 返回下一节点名或 Send 列表。

    派发前为每个子任务登记 PLANNED 任务记录，并把 ``task_id`` / ``generation``
    放进 Send payload，供普通节点内 ``lease`` → deadline → ``complete`` 使用。

    trade 路由只派发**第一个**交易任务，剩余任务作为 ``remaining_tasks`` 尾巴放进
    Send payload；后续由 ``route_next_trade`` 在上一任务 VERIFIED 后逐个派发。

    Args:
        state: 父图 state。
        broker: 可选事件总线，用于发布派发事件。
        task_store: 可选任务状态存储，用于登记子任务租约记录。

    Returns:
        下一节点名、Send 列表或 ``main_agent``。
    """
    decision = PlannerDecision(**state["decision"])
    # 无需工具：直接回到 main_agent 生成最终回复。
    if decision.route == "direct":
        return "main_agent"
    # planner 说 trade 却没给任务：当作普通对话，避免派发空子图。
    if decision.route == "trade":
        # planner_node 已把完整 trade 任务列表写进 remaining_tasks；缺省回退到
        # decision.tasks，保证单独调用 route_after_planner 的旧用法兼容。
        queued = state.get("remaining_tasks")
        if queued is None:
            queued = [task.model_dump() for task in decision.tasks]
        tasks = [dict(task) for task in queued]
        if not tasks:
            return "main_agent"
        subtask, tail = tasks[0], tasks[1:]
        task_id = subtask_task_id("trade_agent", subtask, 0)
        await _register_subtask(task_store, task_id, "trade_agent")
        sends = [
            Send(
                "trade_agent",
                {
                    "subtask": subtask,
                    "remaining_tasks": tail,
                    "task_id": task_id,
                    "generation": 1,
                    "chain_index": 0,
                    "summary": task_context(state),
                },
            )
        ]
        _publish_dispatch(broker, sends)
        return sends
    # Send 的 payload 不会自动继承父图 channel，因此显式把已算好的 preference_hint
    # 透传给 search 子图；trade 子图 inject_preference=False，不需要偏好。
    sends = []
    for index, task in enumerate(decision.tasks or []):
        subtask = task.model_dump()
        task_id = subtask_task_id("search_agent", subtask, index)
        await _register_subtask(task_store, task_id, "search_agent")
        sends.append(
            Send(
                "search_agent",
                {
                    "subtask": subtask,
                    "preference_hint": state.get("preference_hint", ""),
                    "summary": task_context(state),
                    "task_id": task_id,
                    "generation": 1,
                },
            )
        )
    if not sends:
        return "main_agent"
    _publish_dispatch(broker, sends)
    return sends


async def route_next_trade(
    state: MainState,
    broker=None,
    task_store=None,
    max_steps: int | None = None,
):
    """``trade_agent`` 之后的条件边：VERIFIED 才继续，失败/超时/fencing 停链。

    判定顺序：
    1. 有 ``task_store`` 时以存储状态为准：只有 VERIFIED 才继续（FAILED / UNKNOWN /
       fencing 拒绝后停在 PLANNED 都会停链）；
    2. 无存储或缺少 task_id 时回退到失败证据：``task_failures`` 里有当前 task_id，
       或最后一条消息以 ``[subagent unavailable]`` 开头；
    3. ``remaining_tasks`` 非空且未超步数预算才派发下一个，并带上新尾巴与递增
       chain_index。

    Args:
        state: 父图 state（含 trade 节点回写的 trade_chain_step / remaining_tasks /
            task_failures）。
        broker: 可选事件总线，用于发布下一个派发事件。
        task_store: 可选任务状态存储，用于读取上一步 VERIFIED 状态。
        max_steps: 单次链最多执行的子任务数；None 或非正值表示不额外限制。

    Returns:
        ``main_agent``（停止）或 ``Send("trade_agent", ...)`` 列表（继续）。
    """
    step = state.get("trade_chain_step") or {}
    remaining = [dict(task) for task in (state.get("remaining_tasks") or [])]
    task_id = str(step.get("task_id") or "")
    chain_index = int(step.get("chain_index") or 0)

    # 1) 有存储时以 TaskStateStore 状态为准：只有 VERIFIED 才允许继续。
    #    FAILED / UNKNOWN / fencing（generation 被推高后停在 PLANNED）都会停链。
    if task_store is not None and task_id:
        try:
            record = await task_store.get(task_id)
        except Exception:  # noqa: BLE001 - 查询失败按未知处理，fail-closed 停链
            return "main_agent"
        if record is None or _status_text(record.status) != TaskStatus.VERIFIED.value:
            return "main_agent"
    # 2) 无存储（或缺少 task_id）时回退到节点上报的失败 / 消息标记。
    else:
        failures = state.get("task_failures") or []
        if task_id and any(
            isinstance(entry, dict) and entry.get("task_id") == task_id
            for entry in failures
        ):
            return "main_agent"
        if _last_message_content(state.get("messages")).startswith(SUBAGENT_UNAVAILABLE):
            return "main_agent"

    # 3) 步数预算：已执行 chain_index+1 步，达到上限即停链。
    if max_steps is not None and int(max_steps) > 0 and chain_index + 1 >= int(max_steps):
        return "main_agent"
    if not remaining:
        return "main_agent"

    subtask, tail = remaining[0], remaining[1:]
    next_index = chain_index + 1
    next_task_id = subtask_task_id("trade_agent", subtask, next_index)
    await _register_subtask(task_store, next_task_id, "trade_agent")
    sends = [
        Send(
            "trade_agent",
            {
                "subtask": subtask,
                "remaining_tasks": tail,
                "task_id": next_task_id,
                "generation": 1,
                "chain_index": next_index,
                "summary": task_context(state),
            },
        )
    ]
    _publish_dispatch(broker, sends)
    return sends


def make_planner_router(broker=None, task_store=None):
    """把 broker / task_store 绑定到 ``route_after_planner`` 的条件边函数。"""

    async def route(state: MainState):
        return await route_after_planner(state, broker, task_store)

    return route


def make_trade_router(broker=None, task_store=None, max_steps: int | None = None):
    """把 broker / task_store / 步数预算绑定到 ``route_next_trade`` 的条件边函数。"""

    async def route(state: MainState):
        return await route_next_trade(state, broker, task_store, max_steps)

    return route
