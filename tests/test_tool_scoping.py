# -*- coding: utf-8 -*-
"""P12-2 工具权限收紧：按图断言工具名，并验证 bootstrap 的读写分层。

约束（见 docs/superpowers/specs/2026-10-12-ecommerce-agent-p12-design.md 第 4 节）：

- ``main_agent``：只读兜底工具 ``product_search`` / ``category_insight_tool`` /
  ``query_order`` / ``web_search_tool``，**不含** ``create_order`` / ``cancel_order``；
- ``trade_agent``：``create_order`` / ``cancel_order`` / ``query_order``；
- ``search_agent``：``product_search`` / ``category_insight_tool`` / ``web_search_tool``。

测试覆盖两条链路：

1. ``build_main_graph`` 的实际绑定（main_tools ToolNode 的名字集合 + 主 / search /
   trade 三个 LLM 的 ``bind_tools`` 入参）；
2. ``bootstrap.build_container`` 传给 ``build_main_graph`` 的参数分层（只读工具进
   main，订单写工具只进 trade），跑真实 bootstrap 但把基础设施打桩。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.application.agents.main_graph import build_main_graph
from app.application.agents.subagents import PlannerDecision

#: 只读兜底工具名：允许出现在 main_agent。
READ_ONLY_TOOL_NAMES = {
    "product_search",
    "category_insight_tool",
    "query_order",
    "web_search_tool",
}

#: 订单写工具名：只允许出现在 trade_agent。
ORDER_WRITE_TOOL_NAMES = {"create_order", "cancel_order"}

#: P10 偏好记忆工具名：main_agent 专属用户记忆写工具；trade_agent 不得拿到。
PREFERENCE_TOOL_NAMES = {"remember_preference_tool", "forget_preference_tool"}


@tool
def product_search(query: str) -> str:
    """搜索商品。"""
    return f"商品检索结果：{query}"


@tool
def category_insight_tool(question: str) -> str:
    """品类购买知识。"""
    return f"品类知识：{question}"


@tool
def query_order(order_id: str) -> str:
    """按订单号查询订单详情（只读）。"""
    return f"订单快照：{order_id}"


@tool
def create_order(order_id: str) -> str:
    """创建订单（写）。"""
    return f"已下单：{order_id}"


@tool
def cancel_order(order_id: str) -> str:
    """取消订单（写）。"""
    return f"已取消：{order_id}"


@tool
def web_search_tool(query: str) -> str:
    """联网搜索补充信息。"""
    return f"联网结果：{query}"


@tool
def remember_preference_tool(statement: str) -> str:
    """记住买家长期偏好（写用户记忆）。"""
    return f"已记住：{statement}"


@tool
def forget_preference_tool(statement: str) -> str:
    """撤回买家长期偏好（写用户记忆）。"""
    return f"已撤回：{statement}"


READ_ONLY_TOOLS = [product_search, category_insight_tool, query_order, web_search_tool]
PREFERENCE_TOOLS = [remember_preference_tool, forget_preference_tool]
ORDER_TOOLS = [query_order, create_order, cancel_order]


def _names(tools) -> set[str]:
    """把工具对象列表折算成名字集合。"""
    return {getattr(tool, "name", str(tool)) for tool in tools or []}


class _FakePlanner:
    """返回固定 direct 决策的 planner 替身。"""

    def __init__(self, decision: PlannerDecision | None = None) -> None:
        self._decision = decision or PlannerDecision(
            route="direct", reasoning="简单咨询", tasks=[]
        )

    def with_structured_output(self, schema, method=None):
        return self

    async def ainvoke(self, messages):
        return self._decision


class _RecordingLlm:
    """LLM 替身：记录 ``bind_tools`` 入参，便于按图断言工具集合。"""

    def __init__(self, reply: str = "完成") -> None:
        self._reply = reply
        self.bound_tools: list | None = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages):
        return AIMessage(content=self._reply)


def _settings():
    """最小 Settings 替身：只暴露 build_main_graph 读取的字段。"""
    return SimpleNamespace(context_trigger_tokens=10_000, context_keep_recent=8)


def _tool_node_names(graph, node_name: str) -> set[str]:
    """从主图 builder 的 ToolNode 实例里取工具名集合。"""
    node = graph.builder.nodes[node_name]
    runnable = getattr(node, "runnable", None)
    tools_by_name = getattr(runnable, "_tools_by_name", None)
    if tools_by_name is None:
        from langgraph.prebuilt import ToolNode

        assert isinstance(runnable, ToolNode), f"{node_name} 不是 ToolNode"
        tools_by_name = runnable.tools_by_name
    return set(tools_by_name)


def _build_scoped_graph(
    *,
    all_tools,
    main_tools,
    search_tools,
    trade_tools,
    decision: PlannerDecision | None = None,
    tracer=None,
):
    """构建一个真实主图，返回 ``(graph, main_llm, search_llm, trade_llm)``。"""
    main_llm = _RecordingLlm("直接回答")
    search_llm = _RecordingLlm("检索完成")
    trade_llm = _RecordingLlm("交易完成")
    graph = build_main_graph(
        _settings(),
        main_llm,
        _FakePlanner(decision),
        search_llm=search_llm,
        trade_llm=trade_llm,
        all_tools=all_tools,
        main_tools=main_tools,
        search_tools=search_tools,
        trade_tools=trade_tools,
        checkpointer=InMemorySaver(),
        tracer=tracer,
    )
    return graph, main_llm, search_llm, trade_llm


# --------------------------------------------------------------------------- #
# 1. build_main_graph：按图断言工具名
# --------------------------------------------------------------------------- #

async def test_main_agent_binds_only_read_only_tools():
    """主 Agent 的 bind_tools 入参只能是只读工具（+ 用户记忆写工具）。"""
    _, main_llm, _, _ = _build_scoped_graph(
        all_tools=[*READ_ONLY_TOOLS, *PREFERENCE_TOOLS, *ORDER_TOOLS],
        main_tools=[*READ_ONLY_TOOLS, *PREFERENCE_TOOLS],
        search_tools=READ_ONLY_TOOLS,
        trade_tools=ORDER_TOOLS,
    )
    assert _names(main_llm.bound_tools) == (READ_ONLY_TOOL_NAMES | PREFERENCE_TOOL_NAMES)
    assert _names(main_llm.bound_tools).isdisjoint(ORDER_WRITE_TOOL_NAMES)


async def test_main_tools_node_excludes_order_write_tools():
    """main_tools ToolNode 里不能出现 create_order / cancel_order。"""
    graph, _, _, _ = _build_scoped_graph(
        all_tools=[*READ_ONLY_TOOLS, *ORDER_TOOLS],
        main_tools=READ_ONLY_TOOLS,
        search_tools=READ_ONLY_TOOLS,
        trade_tools=ORDER_TOOLS,
    )
    names = _tool_node_names(graph, "main_tools")
    assert names == READ_ONLY_TOOL_NAMES
    assert names.isdisjoint(ORDER_WRITE_TOOL_NAMES)


async def test_trade_agent_owns_order_write_tools():
    """create_order / cancel_order 只出现在 trade_agent 的工具集里。"""
    graph, _, _, trade_llm = _build_scoped_graph(
        all_tools=[*READ_ONLY_TOOLS, *ORDER_TOOLS],
        main_tools=READ_ONLY_TOOLS,
        search_tools=READ_ONLY_TOOLS,
        trade_tools=ORDER_TOOLS,
    )
    trade_names = _names(trade_llm.bound_tools)
    assert trade_names == {"create_order", "cancel_order", "query_order"}
    assert ORDER_WRITE_TOOL_NAMES <= trade_names
    # 主图 ToolNode 与主 Agent 都不含写工具：写操作只能走 trade_agent。
    assert ORDER_WRITE_TOOL_NAMES.isdisjoint(_tool_node_names(graph, "main_tools"))


async def test_search_agent_tools_stay_read_only():
    """search_agent 只拿检索类工具，不含订单写工具。"""
    _, _, search_llm, _ = _build_scoped_graph(
        all_tools=[*READ_ONLY_TOOLS, *ORDER_TOOLS],
        main_tools=READ_ONLY_TOOLS,
        search_tools=[product_search, category_insight_tool, web_search_tool],
        trade_tools=ORDER_TOOLS,
    )
    search_names = _names(search_llm.bound_tools)
    assert search_names == {"product_search", "category_insight_tool", "web_search_tool"}
    assert search_names.isdisjoint(ORDER_WRITE_TOOL_NAMES)


async def test_read_only_tools_still_reach_main_agent_direct_route():
    """direct 路由下 main_agent 仍能完成一次调用，且只读工具保持可用。"""
    graph, main_llm, _, _ = _build_scoped_graph(
        all_tools=READ_ONLY_TOOLS,
        main_tools=READ_ONLY_TOOLS,
        search_tools=READ_ONLY_TOOLS,
        trade_tools=READ_ONLY_TOOLS,
        decision=PlannerDecision(route="direct", reasoning="咨询", tasks=[]),
    )
    assert READ_ONLY_TOOL_NAMES <= _names(main_llm.bound_tools)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="你好")]},
        config={"configurable": {"thread_id": "scoping-direct"}},
    )
    assert result["messages"][-1].content == "直接回答"


async def test_default_main_tools_fall_back_to_all_tools_for_legacy_callers():
    """P12-2 兼容性：旧调用方不传 main_tools 时仍沿用 all_tools。"""
    main_llm = _RecordingLlm("直接回答")
    graph = build_main_graph(
        _settings(),
        main_llm,
        _FakePlanner(),
        [product_search],
        InMemorySaver(),
    )
    assert _names(main_llm.bound_tools) == {"product_search"}
    assert _tool_node_names(graph, "main_tools") == {"product_search"}


# --------------------------------------------------------------------------- #
# 2. bootstrap：读写工具分层（真实 build_main_graph + 打桩基础设施）
# --------------------------------------------------------------------------- #

class _FakeEngine:
    """异步引擎替身：只提供 dispose。"""

    async def dispose(self) -> None:
        return None


class _FakeProductRepo:
    """商品仓储替身：空库 + 可接受灌种子。"""

    async def count(self) -> int:
        return 0

    async def insert_many(self, products) -> None:
        return None

    async def list_all(self):
        return []


class _FakeReranker:
    """reranker 替身：预热 no-op。"""

    async def warmup(self) -> None:
        return None


class _FakeCategoryKb:
    """品类知识库替身：bootstrap no-op。"""

    async def bootstrap(self, knowledge_dir) -> None:
        return None


class _FakeBroker:
    """事件总线替身：aclose no-op。"""

    async def aclose(self) -> None:
        return None


class _FakeStore:
    """通用异步存储 / 基础设施替身：构造即用，close no-op。"""

    async def close(self) -> None:
        return None


class _FakeLlm:
    """LLM 替身：同时支持 bind_tools 与 with_structured_output。"""

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema, method=None):
        return self

    async def ainvoke(self, messages=None):
        return AIMessage(content="ok")


class _FakeCheckpointer:
    """checkpointer 替身：暴露 saver，close no-op。"""

    saver = None

    async def close(self) -> None:
        return None


def _tmp_knowledge_root():
    """PROJECT_ROOT 替身：只需支持 ``/ "knowledge"`` 拼接。"""
    from pathlib import Path

    return Path("/tmp/p12-tool-scoping")


async def _async_noop(*args, **kwargs):
    return None


async def _async_zero(*args, **kwargs):
    return 0


async def _async_fake_checkpointer(url):
    return _FakeCheckpointer()


def _install_bootstrap_stubs(monkeypatch, bootstrap_module) -> None:
    """把 bootstrap 的外部依赖换成轻量替身，只保留工具分层逻辑为真实代码。"""
    for name, factory in (
        ("MySqlProductRepository", lambda engine: _FakeProductRepo()),
        ("MySqlOrderRepository", lambda engine: _FakeStore()),
        ("MySqlTaskStateStore", lambda engine: _FakeStore()),
        ("MySqlMetricsStore", lambda engine: _FakeStore()),
        ("ChromaLocalEmbeddingClient", lambda: _FakeStore()),
        ("ChromaProductIndex", lambda settings: _FakeStore()),
        ("BgeReranker", lambda model: _FakeReranker()),
        ("RedisCache", lambda url: _FakeStore()),
        ("SemanticCache", lambda *a, **k: _FakeStore()),
        ("DisabledSemanticCache", lambda *a, **k: _FakeStore()),
        ("LocalTraceLogger", lambda data_dir, **kwargs: _FakeStore()),
        ("MySqlPreferenceStore", lambda engine: _FakeStore()),
        ("MySqlFeedbackStore", lambda engine: _FakeStore()),
        ("JsonlBadcaseWriter", lambda path: _FakeStore()),
        ("CategoryKnowledgeBase", lambda *a, **k: _FakeCategoryKb()),
        ("GatewayThrottle", lambda *a, **k: _FakeStore()),
        ("CircuitBreakerRegistry", lambda settings: _FakeStore()),
        ("build_chat_model", lambda settings: _FakeLlm()),
        ("build_resilient_tool", lambda tool, *a, **k: tool),
        ("create_engine", lambda url: _FakeEngine()),
    ):
        monkeypatch.setattr(bootstrap_module, name, factory)
    monkeypatch.setattr(bootstrap_module, "bootstrap_schema", _async_noop)
    monkeypatch.setattr(bootstrap_module, "seed_order_counter", _async_zero)
    monkeypatch.setattr(bootstrap_module, "build_checkpointer", _async_fake_checkpointer)
    monkeypatch.setattr(bootstrap_module, "bootstrap_product_index", _async_noop)
    monkeypatch.setattr(bootstrap_module, "EventBroker", lambda **k: _FakeBroker())
    monkeypatch.setattr(bootstrap_module, "PROJECT_ROOT", _tmp_knowledge_root())


def _bootstrap_settings(tavily_api_key: str = ""):
    """build_container 读取的最小 Settings 替身。"""
    return SimpleNamespace(
        database_url="sqlite+aiosqlite:///:memory:",
        data_dir=_tmp_knowledge_root(),
        reranker_enabled=False,
        reranker_model="BAAI/bge-reranker-base",
        redis_url="",
        queue_mode="direct",
        semantic_cache_enabled=False,
        tavily_api_key=tavily_api_key,
        llm_api_key="test-only-llm-secret",
        api_key="",
        buyer_api_keys_json="",
        llm_max_concurrency=4,
        preference_top_k=5,
        subagent_timeout_seconds=5.0,
        subagent_max_retries=1,
        subagent_lease_seconds=10.0,
        trade_chain_max_steps=5,
        context_trigger_tokens=10_000,
        context_keep_recent=8,
        model_route_mode="llm",
        order_confirm_timeout_seconds=1.0,
    )


async def _build_real_container(monkeypatch, tavily_api_key: str = ""):
    """跑一遍真实 build_container（基础设施打桩），返回 Container。"""
    import app.bootstrap as bootstrap_module

    _install_bootstrap_stubs(monkeypatch, bootstrap_module)
    # 延迟取函数引用：确保 monkeypatch 先生效。
    container = await bootstrap_module.build_container(
        _bootstrap_settings(tavily_api_key=tavily_api_key)
    )
    return container


async def test_bootstrap_wires_read_only_tools_to_main_graph(monkeypatch):
    """bootstrap 给主图传只读 main_tools，订单写工具只留给 trade_tools。"""
    container = await _build_real_container(monkeypatch)
    try:
        main_names = _names(container.main_tools)
        trade_names = _names(container.trade_tools)
        all_names = _names(container.tools)

        # 主 Agent：只读四件套（web_search 未配置时缺席）+ 偏好记忆；无订单写工具。
        assert {"product_search", "category_insight_tool", "query_order"} <= main_names
        assert ORDER_WRITE_TOOL_NAMES.isdisjoint(main_names)
        assert main_names <= (READ_ONLY_TOOL_NAMES | PREFERENCE_TOOL_NAMES)

        # trade：订单三件套齐全，且不碰检索 / 记忆工具。
        assert trade_names == {"create_order", "query_order", "cancel_order"}

        # all_tools 仍是完整清单（既有契约：smoke 按名取 create_order）。
        assert ORDER_WRITE_TOOL_NAMES <= all_names
        assert all_names == (main_names | trade_names)

        # 真实主图 main_tools ToolNode 与分层一致。
        assert _tool_node_names(container.agent, "main_tools") == main_names
        assert ORDER_WRITE_TOOL_NAMES.isdisjoint(
            _tool_node_names(container.agent, "main_tools")
        )
    finally:
        await container.close()


async def test_bootstrap_web_search_only_joins_read_only_tools(monkeypatch):
    """配置 TAVILY_API_KEY 时 web_search 进 main，不进 trade。"""
    container = await _build_real_container(monkeypatch, tavily_api_key="tvly-test")
    try:
        main_names = _names(container.main_tools)
        trade_names = _names(container.trade_tools)
        assert "web_search_tool" in main_names
        assert "web_search_tool" not in trade_names
        assert ORDER_WRITE_TOOL_NAMES.isdisjoint(main_names)
    finally:
        await container.close()


async def test_bootstrap_without_tavily_key_omits_web_search(monkeypatch):
    """未配置 TAVILY_API_KEY 时静默降级：不注册 web_search，其余分层不变。"""
    container = await _build_real_container(monkeypatch, tavily_api_key="")
    try:
        main_names = _names(container.main_tools)
        assert "web_search_tool" not in main_names
        assert {"product_search", "category_insight_tool", "query_order"} <= main_names
        assert ORDER_WRITE_TOOL_NAMES.isdisjoint(main_names)
        assert _names(container.trade_tools) == {"create_order", "query_order", "cancel_order"}
    finally:
        await container.close()
