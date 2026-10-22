# -*- coding: utf-8 -*-
"""子 Agent 公共基座：任务规格、子图工厂与租约 / fencing 节点工厂。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import nullcontext
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field

from app.application.agents.state import MainState
from app.application.evidence.task_state import TaskStateStore, TaskStatus
from app.infrastructure.context import ShoppingContext
from app.infrastructure.llm import bind_tools
from app.infrastructure.tracing import traced_span

# 子 Agent 不可用时统一错误前缀，主 Agent 据此如实告知用户。
SUBAGENT_UNAVAILABLE = "[subagent unavailable]"


class SubTaskSpec(BaseModel):
    """子任务规格：目标子 Agent 与自包含指令。"""

    agent: Literal["search_agent", "trade_agent"]              # 目标子 Agent
    instruction: str = Field(description="子任务自包含指令")  # 子任务指令


class PlannerDecision(BaseModel):
    """planner 结构化输出：路由类型、理由与子任务列表。"""

    route: Literal["direct", "search", "trade", "search_parallel"]  # 路由类型
    reasoning: str                                                  # 简短理由，用于 trace / 调试
    tasks: list[SubTaskSpec] = []                                   # 子任务列表；direct 时为空


def _subagent_graph(
    system_prompt: str,
    llm,
    tools,
    name: str,
    inject_preference: bool,
    state_schema=MainState,
    throttle=None,
    tracer=None,
):
    """构建 ``prepare → agent → (tools → agent)*`` 的子图，prepare 负责上下文隔离。

    Args:
        system_prompt: 子 Agent 的 system prompt。
        llm: 子 Agent 使用的 LLM。
        tools: 子图可用工具。
        name: 图名（日志 / 可视化用）。
        inject_preference: 是否把父图偏好 hint 注入子图。
        state_schema: 子图 state schema。
        throttle: 可选 LLM 并发闸门。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        编译后的子图。
    """
    llm_with_tools = bind_tools(llm, tools)

    async def prepare_node(state: Any) -> dict:
        """子图上下文隔离：清掉父历史，只留自包含指令和背景摘要。"""
        from langgraph.graph.message import RemoveMessage

        spec = SubTaskSpec(**state["subtask"])
        removals = []
        for message in state.get("messages", []):
            # RemoveMessage 必须带 id；历史消息若缺 id 先补一个再删。
            if getattr(message, "id", None) is None:
                message.id = str(uuid.uuid4())
            removals.append(RemoveMessage(id=message.id, content=""))
        new_messages = [HumanMessage(content=spec.instruction)]
        if state.get("summary"):
            new_messages.append(SystemMessage(content=f"背景摘要：{state['summary']}"))
        preference_hint = state.get("preference_hint")
        if inject_preference and preference_hint:
            new_messages.append(SystemMessage(content=preference_hint))
        return {"messages": [*removals, *new_messages], "turn_compressed": False}

    async def agent_node(state: Any) -> dict:
        """调用绑定工具的 LLM，把回复追加回子图 messages。

        Args:
            state: 子图 state。

        Returns:
            ``{'messages': [AIMessage]}`` 局部更新。
        """
        messages = [SystemMessage(content=system_prompt), *state["messages"]]
        async with traced_span(tracer, name, {"node": "agent"}) as span:
            slot = throttle.slot() if throttle is not None else nullcontext()
            async with slot:
                response = await llm_with_tools.ainvoke(messages)
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["message_count"] = len(messages)
        return {"messages": [response]}

    builder = StateGraph(state_schema)
    builder.add_node("prepare", prepare_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(name=name)


def subtask_task_id(node: str, subtask: dict, index: int = 0) -> str:
    """为一次子 Agent 派发生成稳定 task_id。

    scope 依次取当前 trace_id / session_id / ``anonymous``，保证同一请求内同一
    子任务可重复计算出一致 id（用于路由登记与节点租约对齐），且不跨请求串台。

    Args:
        node: 子 Agent 节点名（``search_agent`` / ``trade_agent``）。
        subtask: 子任务规格 dict。
        index: 同批派发内的序号，避免同指令子任务撞 id。

    Returns:
        task_id 字符串（截断到 191 字符以内）。
    """
    scope = (
        ShoppingContext.current_trace_id()
        or ShoppingContext.current_session_id()
        or "anonymous"
    )
    digest = hashlib.sha256(
        json.dumps(subtask, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"subagent:{node}:{scope}:{index}:{digest}"[:191]


def build_subagent_node(
    name: str,
    subgraph,
    task_store: TaskStateStore | None = None,
    timeout_seconds: float = 120.0,
    max_retries: int = 2,
    lease_seconds: float = 180.0,
):
    """把编译好的子图包成父图普通节点：租约 → deadline → 重试 → fencing 提交。

    每次尝试前 ``task_store.lease``（重试 generation+1）；尝试内用
    ``asyncio.wait_for(subgraph.ainvoke(...), timeout)`` 施加硬 deadline。
    成功 ``complete(task_id, generation, VERIFIED)``，旧代次提交会被 fencing
    拒绝并返回明确错误；超时标记 ``UNKNOWN`` 且不重试（不重跑可能已发生的写
    操作）；只有明确异常才重试，超过 ``max_retries`` 返回
    ``[subagent unavailable]``。

    Args:
        name: 节点 / 子 Agent 名，同时作为 task action。
        subgraph: 编译好的子图（或测试替身，需实现 ``ainvoke``）。
        task_store: 任务状态存储；为 None 时跳过租约与 fencing。
        timeout_seconds: 单次尝试的 deadline。
        max_retries: 明确失败允许的额外重试次数上限。
        lease_seconds: 每次尝试的租约时长。

    Returns:
        ``async def run(state) -> dict`` 形式的父图节点函数。
    """
    retries = max(0, int(max_retries))
    timeout = float(timeout_seconds)
    lease = float(lease_seconds)

    def _error(text: str) -> dict:
        """把错误文本包装成单条 AIMessage，作为父图节点输出。"""
        return {"messages": [AIMessage(content=text)]}

    async def _complete(task_id: str, generation: int, status: TaskStatus) -> bool:
        """提交任务终态；task_store 缺失视为成功，落库异常按 fencing 失败处理。"""
        if task_store is None:
            return True
        try:
            return await task_store.complete(task_id, generation, status=status)
        except Exception:  # noqa: BLE001 - 落库失败按 fencing 失败处理，不阻断父图
            return False

    async def run(state: dict) -> dict:
        """父图普通节点：执行一次带租约与 deadline 的子 Agent 调用。

        Args:
            state: Send payload 展开的父图局部状态（subtask / task_id / generation 等）。

        Returns:
            ``{'messages': [...]}`` 局部更新；失败时含 ``[subagent unavailable]`` 文本。
        """
        subtask = state.get("subtask") or {}
        task_id = state.get("task_id") or subtask_task_id(name, subtask)
        generation = int(state.get("generation") or 1)
        substate: dict[str, Any] = {"subtask": subtask}
        for key in ("preference_hint", "summary"):
            if state.get(key) is not None:
                substate[key] = state[key]
        config = {"configurable": {"thread_id": task_id}}

        attempts = retries + 1
        last_error = ""
        for attempt in range(attempts):
            if attempt > 0:
                # 每次重试递增 generation；旧尝试的提交会被 fencing 拒绝。
                generation += 1
            if task_store is not None:
                try:
                    leased = await task_store.lease(task_id, generation, lease)
                except Exception as exc:  # noqa: BLE001
                    return _error(f"{SUBAGENT_UNAVAILABLE} {name} 租约失败：{exc}")
                if not leased:
                    # 已有更高代次在跑，本节点放弃执行，避免双写。
                    return _error(
                        f"{SUBAGENT_UNAVAILABLE} {name} 任务 {task_id} 租约已被更高代次接管"
                        f"（generation {generation}）"
                    )
            try:
                result = await asyncio.wait_for(
                    subgraph.ainvoke(substate, config), timeout=timeout
                )
            except asyncio.TimeoutError:
                # 超时结果未知：可能已产生副作用，标记 UNKNOWN 且不自动重试。
                await _complete(task_id, generation, TaskStatus.UNKNOWN)
                return _error(
                    f"{SUBAGENT_UNAVAILABLE} {name} 执行超时（>{timeout:g}s），"
                    f"任务 {task_id} 已标记 UNKNOWN"
                )
            except Exception as exc:  # noqa: BLE001 - 只有明确异常才重试
                last_error = str(exc) or exc.__class__.__name__
                continue
            # 成功也要按当前 generation 提交，过期提交会被拒绝。
            committed = await _complete(task_id, generation, TaskStatus.VERIFIED)
            if not committed:
                # 提交时发现租约已被更高代次接管，说明结果已过期。
                return _error(
                    f"{SUBAGENT_UNAVAILABLE} {name} 提交被 generation fencing 拒绝"
                    f"（generation {generation} 已过期）"
                )
            messages = result.get("messages") if isinstance(result, dict) else None
            return {"messages": list(messages or [])}

        # 明确异常重试耗尽：落 FAILED，向父图返回统一不可用标记。
        await _complete(task_id, generation, TaskStatus.FAILED)
        return _error(f"{SUBAGENT_UNAVAILABLE} {name} 连续失败 {attempts} 次：{last_error}")

    return run
