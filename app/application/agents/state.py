# -*- coding: utf-8 -*-
"""父图 / 子图 state schema 与并行 fan-in reducer。

P14 重构：把原先散落在 ``subagents.py`` 的 state schema 与 reducer 集中到这里，
节点实现只依赖本模块，不再反向依赖图装配。
"""
from __future__ import annotations

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def _last_update(left, right):
    """LangGraph 并行 fan-in 的标量 reducer：保留最新一次写入的值。"""
    return right if right is not None else left


def _merge_task_failures(left, right):
    """``task_failures`` 的累加 reducer：逐条合并，并按 ``(task_id, status)`` 去重。

    trade 串行链每一步只写一条失败记录；fan-in 或父图重放时同一记录可能被
    写入多次，去重可避免失败列表随重放膨胀。
    """
    merged = list(left or [])
    seen = {
        (item.get("task_id"), item.get("status"))
        for item in merged
        if isinstance(item, dict)
    }
    for item in right or []:
        key = (
            (item.get("task_id"), item.get("status"))
            if isinstance(item, dict)
            else item
        )
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


class MainState(TypedDict):
    """父图 state schema；会被并发写的标量通道附加 ``_last_update`` reducer。"""

    messages: Annotated[list[AnyMessage], add_messages]  # 对话消息，add_messages 负责合并
    subtask: Annotated[dict, _last_update]               # 当前子任务规格；并行分支都会写，需 reducer
    decision: dict                                       # planner 决策（route / reasoning / tasks）
    user_request: str                                   # 本轮原始请求，避免内部子任务指令取代用户意图
    summary: str                                         # 压缩后的历史摘要
    preference_hint: Annotated[str, _last_update]        # 买家偏好提示；并行 fan-in 需 reducer
    turn_compressed: Annotated[bool, _last_update]       # 本轮是否已压缩；并行写需 reducer
    # P12-1 trade 串行链：planner 写完整任务列表，trade 节点回写「当前任务之后的尾巴」，
    # 条件边据此逐个派发；_last_update 保证重放/并行写时以最新尾巴为准。
    remaining_tasks: Annotated[list[dict], _last_update]
    # P12-1 串行链游标：trade 节点回写刚完成任务的 id / generation / chain_index。
    # 条件边据此查 TaskStateStore 的 VERIFIED 状态、判断步数预算并生成下一个 task_id。
    trade_chain_step: Annotated[dict, _last_update]
    # P12-1 失败证据：trade 链每一步失败（FAILED/UNKNOWN/fencing 拒绝）追加一条，
    # 供条件边停链判定与主 Agent 如实汇报；默认空列表。
    task_failures: Annotated[list[dict], _merge_task_failures]


class TradeSubState(TypedDict):
    """交易子图 schema：刻意不含 preference_hint。

    若把该字段放进 trade 子图，窄分支 fan-in 时可能把空提示写回父图并覆盖偏好。
    """

    messages: Annotated[list[AnyMessage], add_messages]  # 子图消息
    subtask: Annotated[dict, _last_update]               # 子任务规格
    summary: str                                         # 背景摘要
    turn_compressed: Annotated[bool, _last_update]       # 本轮是否已压缩
