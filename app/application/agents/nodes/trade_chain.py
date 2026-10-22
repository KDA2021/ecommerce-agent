# -*- coding: utf-8 -*-
"""trade 串行链节点：执行一次 trade 子任务并把链式记账回写父图。"""
from __future__ import annotations

from app.application.agents.nodes.routes import _inspect_trade_result
from app.application.agents.subagents.base import subtask_task_id


def make_trade_chain_node(run_trade_agent, task_store=None):
    """构造 trade 串行链节点；子 Agent 节点与 task_store 显式传入。

    Args:
        run_trade_agent: ``build_subagent_node`` 产出的子节点函数。
        task_store: 任务状态存储，用于读取/登记租约状态，可为 None。

    Returns:
        ``async def run_trade_chain(state) -> dict``。
    """

    async def run_trade_chain(state: dict) -> dict:
        """执行一次 trade 子任务，并把串行链状态回写父图。

        LangGraph 的 Send payload 不会自动写入父图 channel，且节点写未声明 channel
        会被忽略，因此这里只回写 ``MainState`` 已声明的链式通道：
        ``remaining_tasks``（下一条尾巴）、``trade_chain_step``（刚完成任务的游标）
        与 ``task_failures``（失败证据），让 ``route_next_trade`` 能判断上一步状态
        并续派下一个任务。

        Args:
            state: Send payload 展开的局部状态。

        Returns:
            子 Agent 消息更新 + 串行链记账字段。
        """
        update = await run_trade_agent(state)
        subtask = state.get("subtask") or {}
        task_id = str(state.get("task_id") or subtask_task_id("trade_agent", subtask))
        generation = int(state.get("generation") or 1)
        update["remaining_tasks"] = [
            dict(task) for task in (state.get("remaining_tasks") or [])
        ]
        update["subtask"] = subtask
        update["trade_chain_step"] = {
            "task_id": task_id,
            "generation": generation,
            "chain_index": int(state.get("chain_index") or 0),
        }
        failure = await _inspect_trade_result(
            task_store, task_id, generation, update.get("messages")
        )
        if failure is not None:
            update["task_failures"] = [failure]
        return update

    return run_trade_chain
