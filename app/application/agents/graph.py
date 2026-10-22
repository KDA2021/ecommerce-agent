# -*- coding: utf-8 -*-
"""主图装配：preference → compress → planner → main_agent（含 search/trade 子图与 ToolNode）。

本模块只做图装配（add_node / add_edge）；节点实现位于 ``nodes/``、子图位于
``subagents/``、state schema 位于 ``state.py``。

同时 re-export P0 单 Agent 图（``build_agent_graph`` / ``AgentState`` /
``invoke_agent``），保证 ``from app.application.agents.graph import ...`` 旧导入不变。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.application.agents.nodes.compress import make_compress_node
from app.application.agents.nodes.main_agent import make_main_agent_node
from app.application.agents.nodes.planner import make_planner_node
from app.application.agents.nodes.preference import make_preference_node
from app.application.agents.nodes.routes import make_planner_router, make_trade_router
from app.application.agents.nodes.trade_chain import make_trade_chain_node
from app.application.agents.single_agent import (  # noqa: F401 - 兼容旧导入
    AgentState,
    build_agent_graph,
    invoke_agent,
)
from app.application.agents.state import MainState
from app.application.agents.subagents.base import build_subagent_node
from app.application.agents.subagents.search import build_search_agent_graph
from app.application.agents.subagents.trade import build_trade_agent_graph


def build_main_graph(
    settings,
    main_llm,
    planner_llm,
    *args,
    search_llm=None,
    trade_llm=None,
    all_tools=None,
    search_tools=None,
    trade_tools=None,
    main_tools=None,
    checkpointer=None,
    compression_llm=None,
    broker=None,
    preference_store=None,
    preference_top_k=5,
    throttle=None,
    tracer=None,
    task_store=None,
):
    """Build the planner + main/search/trade graph.

    Two call forms are supported:
    - brief public form: ``(settings, main_llm, planner_llm, all_tools, checkpointer)``
    - explicit wiring form: ``(settings, main_llm, planner_llm, search_llm, trade_llm,
      all_tools, search_tools, trade_tools, checkpointer, compression_llm=None)``

    Args:
        settings: 运行时配置。
        main_llm: 主 Agent LLM。
        planner_llm: planner LLM。
        search_llm: search 子图 LLM；缺省复用 main_llm。
        trade_llm: trade 子图 LLM；缺省复用 main_llm。
        all_tools: 全量工具列表（Container 完整清单，含写工具）。
        search_tools: search 子图工具列表。
        trade_tools: trade 子图工具列表。
        main_tools: 主 Agent 绑定工具列表；缺省复用 all_tools 以兼容旧调用方。
            P12-2 起生产路径由 bootstrap 传入只读工具集，写工具只留在 trade_tools。
        checkpointer: LangGraph checkpointer，可为 None。
        compression_llm: 压缩摘要 LLM；缺省复用 main_llm。
        broker: 事件总线，可为 None。
        preference_store: 偏好存储，可为 None。
        task_store: 任务状态存储，用于子 Agent 租约 / generation fencing，可为 None。
        preference_top_k: 注入的 like 偏好条数上限。
        throttle: LLM 并发闸门，可为 None。
        tracer: 可选 LocalTraceLogger / span tracer。
    """
    # 兼容两种历史调用形式：简写 (all_tools, checkpointer, broker) 与显式装配形式。
    if args:
        if len(args) == 3 and all_tools is None and checkpointer is None and broker is None:
            # 简写形式允许第三个位置参数是 broker。
            all_tools, checkpointer, broker = args
        elif len(args) == 2 and all_tools is None and checkpointer is None:
            all_tools, checkpointer = args
        elif len(args) in (6, 7):
            if len(args) == 6:
                supplied = (
                    search_llm, trade_llm, all_tools, search_tools, trade_tools,
                    checkpointer,
                )
            else:
                supplied = (
                    search_llm, trade_llm, all_tools, search_tools, trade_tools,
                    checkpointer, compression_llm,
                )
            # 位置参数与关键字参数混用会让装配关系含混，直接报错。
            if any(value is not None for value in supplied):
                raise TypeError("cannot mix positional and keyword graph wiring arguments")
            if len(args) == 6:
                search_llm, trade_llm, all_tools, search_tools, trade_tools, checkpointer = args
            else:
                (
                    search_llm, trade_llm, all_tools, search_tools, trade_tools,
                    checkpointer, compression_llm,
                ) = args
        else:
            raise TypeError(
                "build_main_graph expects either 5 arguments "
                "(settings, main_llm, planner_llm, all_tools, checkpointer) or the "
                "explicit wiring form"
            )

    # 缺省回退：子图 LLM 复用主 LLM，子图工具默认复用全量工具。
    if all_tools is None:
        all_tools = []
    if search_llm is None:
        search_llm = main_llm
    if trade_llm is None:
        trade_llm = main_llm
    if search_tools is None:
        search_tools = all_tools
    if trade_tools is None:
        trade_tools = all_tools
    # P12-2：主 Agent 默认沿用全量列表；生产路径由 bootstrap 显式传入只读子集。
    if main_tools is None:
        main_tools = all_tools

    # 节点工厂：每个节点的依赖（LLM / settings / broker / task_store / throttle /
    # tracer）都在这里显式注入，节点实现本身不再从图装配里闭包捕获。
    planner_node = make_planner_node(planner_llm, settings, throttle=throttle, tracer=tracer)
    compress_node = make_compress_node(
        compression_llm or main_llm, settings, broker=broker,
        throttle=throttle, tracer=tracer,
    )
    preference_node = make_preference_node(preference_store, preference_top_k)
    main_agent_node = make_main_agent_node(
        main_llm, main_tools, throttle=throttle, tracer=tracer,
    )

    search_graph = build_search_agent_graph(
        search_llm, search_tools, throttle=throttle, tracer=tracer,
    )
    trade_graph = build_trade_agent_graph(
        trade_llm, trade_tools, throttle=throttle, tracer=tracer,
    )

    # P8-3：子图不再直接挂父图，而是包成普通节点，在节点内做租约 + deadline
    # + 重试 + generation fencing；Send 仍指向这两个节点，并行语义不变。
    subagent_timeout = float(getattr(settings, "subagent_timeout_seconds", 120.0))
    subagent_retries = int(getattr(settings, "subagent_max_retries", 2))
    subagent_lease = float(getattr(settings, "subagent_lease_seconds", 180.0))
    run_search_agent = build_subagent_node(
        "search_agent",
        search_graph,
        task_store=task_store,
        timeout_seconds=subagent_timeout,
        max_retries=subagent_retries,
        lease_seconds=subagent_lease,
    )
    run_trade_agent = build_subagent_node(
        "trade_agent",
        trade_graph,
        task_store=task_store,
        timeout_seconds=subagent_timeout,
        max_retries=subagent_retries,
        lease_seconds=subagent_lease,
    )
    run_trade_chain = make_trade_chain_node(run_trade_agent, task_store)

    # P12-1：单次 trade 串行链最多执行多少步；非正值表示不额外限制。
    trade_chain_max_steps = int(getattr(settings, "trade_chain_max_steps", 5) or 0)
    # planner 条件边返回节点名（direct）或 Send 列表（并行派发子 Agent）。
    route_with_broker = make_planner_router(broker, task_store)
    # trade 是 fail-closed 串行链：只有上一步 VERIFIED 才续派，否则回 main_agent。
    route_trade_with_broker = make_trade_router(broker, task_store, trade_chain_max_steps)

    builder = StateGraph(MainState)
    builder.add_node("preference", preference_node)
    builder.add_node("planner", planner_node)
    builder.add_node("compress", compress_node)
    builder.add_node("main_agent", main_agent_node)
    builder.add_node("main_tools", ToolNode(main_tools))
    builder.add_node("search_agent", run_search_agent)
    builder.add_node("trade_agent", run_trade_chain)

    builder.add_edge(START, "preference")
    builder.add_edge("preference", "compress")
    builder.add_edge("compress", "planner")
    builder.add_conditional_edges("planner", route_with_broker)
    # search 仍是 fail-soft：全部并行派发、各自回到 main_agent 汇总 partial。
    builder.add_edge("search_agent", "main_agent")
    builder.add_conditional_edges("trade_agent", route_trade_with_broker)
    builder.add_conditional_edges("main_agent", tools_condition, {"tools": "main_tools", END: END})
    builder.add_edge("main_tools", "main_agent")
    return builder.compile(checkpointer=checkpointer)
