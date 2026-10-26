# -*- coding: utf-8 -*-
"""P8-3 子 Agent 容错：deadline、任务租约、generation fencing 与并行 fan-in。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.application.agents.main_graph import build_main_graph, build_subagent_node
from app.application.agents.subagents import PlannerDecision, SubTaskSpec
from app.application.evidence.task_state import (
    InMemoryTaskStateStore,
    TaskRecord,
    TaskStatus,
)
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot


async def _seed(store, task_id, *, generation: int = 1) -> None:
    """写入一条 PLANNED 任务记录，模拟路由派发时的登记。"""
    await store.put(
        TaskRecord(
            task_id=task_id,
            trace_id="trace-unit",
            action="search_agent",
            generation=generation,
            status=TaskStatus.PLANNED,
            idempotency_key=task_id,
        )
    )


def _subtask(instruction: str = "查找无塑料旅行三件套") -> dict:
    return {"agent": "search_agent", "instruction": instruction}


class _SlowSubgraph:
    """永远慢于 deadline 的子图，用于验证超时路径。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    async def ainvoke(self, state, config=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {"messages": [AIMessage(content="迟到的成功结果")]}


class _FailingSubgraph:
    """前 ``failures`` 次显式抛错，之后成功。"""

    def __init__(self, failures: int = 99) -> None:
        self.failures = failures
        self.calls = 0

    async def ainvoke(self, state, config=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"boom-{self.calls}")
        return {"messages": [AIMessage(content="重试成功")]}


class _RacingSubgraph:
    """执行期间模拟另一个 worker 把同一 task 的 generation 抢到 2。"""

    def __init__(self, store, task_id: str) -> None:
        self.store = store
        self.task_id = task_id

    async def ainvoke(self, state, config=None):
        await self.store.lease(self.task_id, 2, lease_seconds=30.0)
        return {"messages": [AIMessage(content="旧代次的迟到成功结果")]}


async def test_subagent_timeout_marks_unknown_and_returns_error_text():
    store = InMemoryTaskStateStore()
    await _seed(store, "task-timeout")
    slow = _SlowSubgraph(delay=0.3)
    node = build_subagent_node(
        "search_agent",
        slow,
        store,
        timeout_seconds=0.02,
        max_retries=2,
        lease_seconds=30.0,
    )

    update = await node({"subtask": _subtask(), "task_id": "task-timeout", "generation": 1})

    text = update["messages"][0].content
    assert "[subagent unavailable]" in text
    assert "超时" in text
    # 超时属于「不确定」而非「明确失败」：不重试，避免重跑可能已发生的写操作。
    assert slow.calls == 1
    record = await store.get("task-timeout")
    assert record.status is TaskStatus.UNKNOWN
    assert record.generation == 1


async def test_stale_generation_completion_is_rejected_and_old_result_not_written_back():
    store = InMemoryTaskStateStore()
    await _seed(store, "task-fence")
    racing = _RacingSubgraph(store, "task-fence")
    node = build_subagent_node(
        "search_agent",
        racing,
        store,
        timeout_seconds=1.0,
        max_retries=2,
        lease_seconds=30.0,
    )

    update = await node({"subtask": _subtask(), "task_id": "task-fence", "generation": 1})

    text = update["messages"][0].content
    assert "[subagent unavailable]" in text
    assert "迟到" not in text
    record = await store.get("task-fence")
    assert record.generation == 2
    assert record.status is TaskStatus.PLANNED
    # generation fencing 端口语义：旧代次完成必须被拒绝。
    assert await store.complete("task-fence", 1, status=TaskStatus.VERIFIED) is False


async def test_explicit_failures_retry_is_bounded_by_max_retries():
    store = InMemoryTaskStateStore()
    await _seed(store, "task-retry-cap")
    failing = _FailingSubgraph(failures=99)
    node = build_subagent_node(
        "trade_agent",
        failing,
        store,
        timeout_seconds=1.0,
        max_retries=2,
        lease_seconds=30.0,
    )

    update = await node(
        {
            "subtask": {"agent": "trade_agent", "instruction": "下单"},
            "task_id": "task-retry-cap",
            "generation": 1,
        }
    )

    text = update["messages"][0].content
    assert "[subagent unavailable]" in text
    assert failing.calls == 3  # 首次执行 + 2 次重试
    record = await store.get("task-retry-cap")
    assert record.generation == 3
    assert record.status is TaskStatus.FAILED


async def test_explicit_failure_retry_bumps_generation_then_completes():
    store = InMemoryTaskStateStore()
    await _seed(store, "task-retry-ok")
    flaky = _FailingSubgraph(failures=1)
    node = build_subagent_node(
        "search_agent",
        flaky,
        store,
        timeout_seconds=1.0,
        max_retries=2,
        lease_seconds=30.0,
    )

    update = await node({"subtask": _subtask(), "task_id": "task-retry-ok", "generation": 1})

    assert update["messages"][0].content == "重试成功"
    assert flaky.calls == 2
    record = await store.get("task-retry-ok")
    assert record.generation == 2
    assert record.status is TaskStatus.VERIFIED


@tool
def product_search(query: str) -> str:
    """Search products by a natural-language query."""
    return f"商品检索结果：{query}"


class _RecordingMainLlm:
    """记录主 Agent 看到的上下文，并返回固定综合回复。"""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return AIMessage(content=self._reply)


class _BarrierSearchLlm:
    """两个并行分支必须同时进入 ainvoke 才能通过，用来证明并行是真并行。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.both_running = asyncio.Event()
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        snapshot = list(messages)
        self.calls.append(snapshot)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= 2:
            self.both_running.set()
        try:
            # 顺序执行时第一个分支会在这里超时，最终断言必然失败。
            await asyncio.wait_for(self.both_running.wait(), timeout=2.0)
        finally:
            self.active -= 1
        instruction = next(
            (str(m.content) for m in reversed(snapshot) if isinstance(m, HumanMessage)), ""
        )
        return AIMessage(content=f"检索完成：{instruction}")


class _FakePlanner:
    def __init__(self, decision: PlannerDecision) -> None:
        self._decision = decision

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        return self._decision


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        context_trigger_tokens=10_000,
        context_keep_recent=8,
        subagent_timeout_seconds=5.0,
        subagent_max_retries=2,
        subagent_lease_seconds=30.0,
    )


async def test_parallel_sends_still_fan_in_and_verify_task_records():
    tasks = [
        SubTaskSpec(agent="search_agent", instruction="查找无塑料旅行三件套"),
        SubTaskSpec(agent="search_agent", instruction="查找可折叠水杯"),
    ]
    planner = _FakePlanner(
        PlannerDecision(route="search_parallel", reasoning="两个独立检索", tasks=tasks)
    )
    main_llm = _RecordingMainLlm("已综合两个检索结果")
    search_llm = _BarrierSearchLlm()
    store = InMemoryTaskStateStore()
    graph = build_main_graph(
        _settings(),
        main_llm,
        planner,
        search_llm,
        search_llm,
        [product_search],
        [product_search],
        [product_search],
        InMemorySaver(),
        task_store=store,
    )

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="session-1",
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-parallel",
        )
    )
    try:
        reply = await graph.ainvoke(
            {"messages": [HumanMessage(content="帮我比较两个商品")]},
            config={"configurable": {"thread_id": "parallel-resilience-thread"}},
        )
    finally:
        ShoppingContext.reset(token)

    final_text = [str(m.content) for m in reply["messages"] if getattr(m, "content", "")]
    assert "已综合两个检索结果" in final_text
    assert search_llm.max_active == 2
    main_context = "\n".join(str(m.content) for m in main_llm.calls[0])
    assert "检索完成：查找无塑料旅行三件套" in main_context
    assert "检索完成：查找可折叠水杯" in main_context

    verified = await store.list_by_trace("trace-parallel", TaskStatus.VERIFIED)
    assert len(verified) == 2
    assert all(record.generation == 1 for record in verified)


def test_settings_read_subagent_budget_from_env(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("SUBAGENT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("SUBAGENT_MAX_RETRIES", "4")
    monkeypatch.setenv("SUBAGENT_LEASE_SECONDS", "9")
    settings = load_settings()
    assert settings.subagent_timeout_seconds == 7.0
    assert settings.subagent_max_retries == 4
    assert settings.subagent_lease_seconds == 9.0
