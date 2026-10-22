# -*- coding: utf-8 -*-
"""P12-1 trade 串行链：顺序执行、失败/超时停链、search partial 不受影响。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.application.agents.main_graph import build_main_graph, invoke_agent
from app.application.agents.subagents import (
    PlannerDecision,
    SubTaskSpec,
    subtask_task_id,
)
from app.application.evidence.task_state import InMemoryTaskStateStore, TaskStatus
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot


class _FakePlanner:
    """返回固定 PlannerDecision 的 planner 测试替身。"""

    def __init__(self, decision: PlannerDecision) -> None:
        self._decision = decision

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        return self._decision


class _RecordingTradeLlm:
    """trade 子 Agent 替身：记录指令顺序，可按指令注入失败或超时。"""

    def __init__(self, *, fail_on=(), timeout_on=(), sleep_seconds: float = 0.5) -> None:
        self.fail_on = set(fail_on)
        self.timeout_on = set(timeout_on)
        self.sleep_seconds = sleep_seconds
        self.instructions: list[str] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        instruction = ""
        for message in messages:
            if isinstance(message, HumanMessage):
                instruction = str(message.content)
        self.instructions.append(instruction)
        if instruction in self.timeout_on:
            await asyncio.sleep(self.sleep_seconds)
        if instruction in self.fail_on:
            raise RuntimeError(f"trade failed: {instruction}")
        return AIMessage(content=f"交易完成：{instruction}")


class _FencingTradeLlm(_RecordingTradeLlm):
    """在指定指令执行时抢占同一 task 的更高 generation，制造 fencing 拒绝。"""

    def __init__(self, store, task_id: str, fence_instruction: str) -> None:
        super().__init__()
        self.store = store
        self.task_id = task_id
        self.fence_instruction = fence_instruction

    async def ainvoke(self, messages):
        instruction = ""
        for message in messages:
            if isinstance(message, HumanMessage):
                instruction = str(message.content)
        self.instructions.append(instruction)
        if instruction == self.fence_instruction:
            # 模拟另一个 worker 把同一 task 的 generation 推到 2。
            await self.store.lease(self.task_id, 2, lease_seconds=30.0)
        return AIMessage(content=f"交易完成：{instruction}")


class _SearchLlm:
    """search 子 Agent 替身：记录指令，指定指令直接失败。"""

    def __init__(self, *, fail_on=()) -> None:
        self.fail_on = set(fail_on)
        self.instructions: list[str] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        instruction = ""
        for message in messages:
            if isinstance(message, HumanMessage):
                instruction = str(message.content)
        self.instructions.append(instruction)
        if instruction in self.fail_on:
            raise RuntimeError(f"search failed: {instruction}")
        return AIMessage(content=f"检索完成：{instruction}")


class _MainLlm:
    """主 Agent LLM 替身：记录看到的上下文并返回固定回复。"""

    def __init__(self, reply: str = "主回答") -> None:
        self.reply = reply
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return AIMessage(content=self.reply)


def _settings(**overrides):
    values = dict(
        context_trigger_tokens=10_000,
        context_keep_recent=8,
        subagent_timeout_seconds=5.0,
        subagent_max_retries=0,
        subagent_lease_seconds=30.0,
        trade_chain_max_steps=5,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(trace_id: str):
    """设置带 trace_id 的 ShoppingContext，让任务记录可以按 trace 聚合。"""
    return ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="trade-chain-session",
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id=trace_id,
        )
    )


def _trade_decision(instructions: list[str]) -> PlannerDecision:
    return PlannerDecision(
        route="trade",
        reasoning="多步交易",
        tasks=[
            SubTaskSpec(agent="trade_agent", instruction=instruction)
            for instruction in instructions
        ],
    )


def _build(
    decision: PlannerDecision,
    *,
    trade_llm=None,
    search_llm=None,
    task_store=None,
    settings=None,
):
    main_llm = _MainLlm()
    graph = build_main_graph(
        settings or _settings(),
        main_llm,
        _FakePlanner(decision),
        search_llm=search_llm or _SearchLlm(),
        trade_llm=trade_llm or _RecordingTradeLlm(),
        all_tools=[],
        search_tools=[],
        trade_tools=[],
        checkpointer=InMemorySaver(),
        task_store=task_store,
    )
    return graph, main_llm


async def test_three_trade_tasks_run_in_order_and_all_verified():
    """3 个 trade 任务必须逐个经过 trade_agent，顺序正确且全部 VERIFIED。"""
    instructions = ["查询订单 O1", "取消订单 O2", "下单商品 P3"]
    trade_llm = _RecordingTradeLlm()
    store = InMemoryTaskStateStore()
    graph, main_llm = _build(
        _trade_decision(instructions), trade_llm=trade_llm, task_store=store,
    )

    token = _context("trace-trade-order")
    try:
        reply = await invoke_agent(graph, "trade-order-thread", "处理这三件交易")
    finally:
        ShoppingContext.reset(token)

    # 串行顺序：3 条指令严格按 planner 给的顺序进入 trade_agent。
    assert trade_llm.instructions == instructions
    assert len(main_llm.calls) == 1
    assert reply == "主回答"

    records = await store.list_by_trace("trace-trade-order")
    assert len(records) == 3
    assert all(record.status is TaskStatus.VERIFIED for record in records)
    # chain_index 递增：id 互不相同，避免多任务撞同一个租约记录。
    assert len({record.task_id for record in records}) == 3


async def test_second_trade_failure_stops_chain_and_third_is_not_sent():
    """第二个 trade 明确失败 → 第三个不再派发，链停在 FAILED。"""
    instructions = ["查询订单 O1", "取消订单 O2", "下单商品 P3"]
    trade_llm = _RecordingTradeLlm(fail_on={instructions[1]})
    store = InMemoryTaskStateStore()
    graph, _ = _build(
        _trade_decision(instructions),
        trade_llm=trade_llm,
        task_store=store,
        settings=_settings(subagent_max_retries=0),
    )

    token = _context("trace-trade-fail")
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content="处理这三件交易")]},
            config={"configurable": {"thread_id": "trade-fail-thread"}},
        )
    finally:
        ShoppingContext.reset(token)

    # 第二个失败后不再执行第三个。
    assert trade_llm.instructions == instructions[:2]
    assert [task["instruction"] for task in state["remaining_tasks"]] == [instructions[2]]

    records = await store.list_by_trace("trace-trade-fail")
    assert len(records) == 2
    assert sorted(record.status.value for record in records) == ["FAILED", "VERIFIED"]

    failures = state["task_failures"]
    assert len(failures) == 1
    assert failures[0]["status"] == "FAILED"
    failed_ids = {
        record.task_id for record in records if record.status is TaskStatus.FAILED
    }
    assert failures[0]["task_id"] in failed_ids


async def test_trade_timeout_marks_unknown_and_stops_chain():
    """trade 超时 → 标记 UNKNOWN、不重试、不再派发后续任务。"""
    instructions = ["查询订单 O1", "取消订单 O2", "下单商品 P3"]
    trade_llm = _RecordingTradeLlm(
        timeout_on={instructions[1]}, sleep_seconds=0.5,
    )
    store = InMemoryTaskStateStore()
    graph, main_llm = _build(
        _trade_decision(instructions),
        trade_llm=trade_llm,
        task_store=store,
        settings=_settings(subagent_timeout_seconds=0.05),
    )

    token = _context("trace-trade-timeout")
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content="处理这三件交易")]},
            config={"configurable": {"thread_id": "trade-timeout-thread"}},
        )
    finally:
        ShoppingContext.reset(token)

    # 超时不重试：只执行到第二个任务；第三个不派发。
    assert trade_llm.instructions == instructions[:2]
    assert [task["instruction"] for task in state["remaining_tasks"]] == [instructions[2]]

    records = await store.list_by_trace("trace-trade-timeout")
    assert len(records) == 2
    assert sorted(record.status.value for record in records) == ["UNKNOWN", "VERIFIED"]

    failures = state["task_failures"]
    assert len(failures) == 1
    assert failures[0]["status"] == "UNKNOWN"
    # 主 Agent 仍被调用一次，以便如实汇报“只完成了一部分 / 交易结果未知”。
    assert len(main_llm.calls) == 1


async def test_search_partial_keeps_going_when_one_branch_fails():
    """search 仍是 fail-soft：一个分支失败不影响另一个分支回主 Agent 汇总。"""
    search_tasks = [
        SubTaskSpec(agent="search_agent", instruction="找无塑料旅行三件套"),
        SubTaskSpec(agent="search_agent", instruction="找可折叠水杯"),
    ]
    decision = PlannerDecision(route="search_parallel", reasoning="两个独立检索", tasks=search_tasks)
    search_llm = _SearchLlm(fail_on={"找可折叠水杯"})
    store = InMemoryTaskStateStore()
    graph, main_llm = _build(decision, search_llm=search_llm, task_store=store)

    token = _context("trace-search-partial")
    try:
        reply = await invoke_agent(graph, "search-partial-thread", "帮我比较两个商品")
    finally:
        ShoppingContext.reset(token)

    assert sorted(search_llm.instructions) == sorted(
        task.instruction for task in search_tasks
    )
    assert reply == "主回答"

    main_context = "\n".join(str(message.content) for message in main_llm.calls[0])
    # 成功的 partial 结果照常进入主 Agent 上下文；失败分支以不可用标记如实呈现。
    assert "检索完成：找无塑料旅行三件套" in main_context
    assert "找可折叠水杯" in main_context

    records = await store.list_by_trace("trace-search-partial")
    assert sorted(record.status.value for record in records) == ["FAILED", "VERIFIED"]


async def test_fencing_rejection_stops_chain():
    """上一步提交被 generation fencing 拒绝（状态回 PLANNED）→ 停止链。"""
    instructions = ["查询订单 O1", "取消订单 O2", "下单商品 P3"]
    store = InMemoryTaskStateStore()

    token = _context("trace-trade-fence")
    try:
        fenced_task_id = subtask_task_id(
            "trade_agent",
            {"agent": "trade_agent", "instruction": instructions[1]},
            1,
        )
        trade_llm = _FencingTradeLlm(store, fenced_task_id, instructions[1])
        graph, _ = _build(
            _trade_decision(instructions), trade_llm=trade_llm, task_store=store,
        )
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content="处理这三件交易")]},
            config={"configurable": {"thread_id": "trade-fence-thread"}},
        )
    finally:
        ShoppingContext.reset(token)

    assert trade_llm.instructions == instructions[:2]
    record = await store.get(fenced_task_id)
    assert record.generation == 2
    assert record.status is TaskStatus.PLANNED
    assert state["task_failures"][0]["status"] == "PLANNED"
    assert [task["instruction"] for task in state["remaining_tasks"]] == [instructions[2]]


async def test_trade_chain_max_steps_stops_after_budget():
    """TRADE_CHAIN_MAX_STEPS 限制单次链执行步数，避免一次请求跑无限交易。"""
    instructions = ["查询订单 O1", "取消订单 O2", "下单商品 P3"]
    trade_llm = _RecordingTradeLlm()
    store = InMemoryTaskStateStore()
    graph, _ = _build(
        _trade_decision(instructions),
        trade_llm=trade_llm,
        task_store=store,
        settings=_settings(trade_chain_max_steps=2),
    )

    token = _context("trace-trade-budget")
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content="处理这三件交易")]},
            config={"configurable": {"thread_id": "trade-budget-thread"}},
        )
    finally:
        ShoppingContext.reset(token)

    assert trade_llm.instructions == instructions[:2]
    assert [task["instruction"] for task in state["remaining_tasks"]] == [instructions[2]]
    records = await store.list_by_trace("trace-trade-budget")
    assert len(records) == 2


def test_settings_read_trade_chain_max_steps_from_env(monkeypatch):
    """TRADE_CHAIN_MAX_STEPS 可通过环境变量覆盖默认值。"""
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("TRADE_CHAIN_MAX_STEPS", "3")
    assert load_settings().trade_chain_max_steps == 3

