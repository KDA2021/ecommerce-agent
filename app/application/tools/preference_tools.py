# -*- coding: utf-8 -*-
"""偏好记忆工具：remember_preference_tool / forget_preference_tool。"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.domain.buyer.preference import BuyerPreference, PreferenceStore
from app.infrastructure.context import ShoppingContext


class RememberPreferenceArgs(BaseModel):
    """remember_preference_tool 入参。"""

    kind: str = Field(description="like 或 dislike")
    statement: str = Field(description="一句话偏好陈述，如“不要塑料材质”")


class ForgetPreferenceArgs(BaseModel):
    """forget_preference_tool 入参。"""

    statement: str = Field(description="要删除的偏好原文，必须完全一致")


def build_preference_tools(store: PreferenceStore, broker=None):
    """构建偏好写入 / 撤回两个 LangChain 工具。

    Args:
        store: 偏好存储端口。
        broker: 可选事件总线。

    Returns:
        两个 ``StructuredTool`` 的列表。
    """
    async def remember_preference_tool(kind: str, statement: str) -> dict:
        """写入买家长期偏好（同 buyer 同 statement 幂等去重）。

        Args:
            kind: ``like`` 或 ``dislike``。
            statement: 偏好原文。

        Returns:
            ``{'saved': ...}``，校验失败时为 ``{'error': ...}``。
        """
        session_id = ShoppingContext.current_session_id()
        snapshot = ShoppingContext.current()
        buyer_id = snapshot.buyer_id if snapshot else "anonymous"
        if broker:
            broker.publish(session_id, "tool.invoke", {"tool": "remember_preference_tool", "args": {"kind": kind, "statement": statement}})
        try:
            await store.append(BuyerPreference(buyer_id=buyer_id, kind=kind, statement=statement))
        except ValueError as err:
            if broker:
                broker.publish(session_id, "tool.result", {"tool": "remember_preference_tool", "error": str(err)})
            return {"error": str(err)}
        if broker:
            broker.publish(session_id, "tool.result", {"tool": "remember_preference_tool", "saved": statement})
        return {"saved": f"[{kind}] {statement}"}

    async def forget_preference_tool(statement: str) -> dict:
        """按原文精确撤回偏好，并返回剩余偏好。

        Args:
            statement: 要删除的偏好原文。

        Returns:
            ``{'deleted': bool, 'remaining': [...]}``。
        """
        session_id = ShoppingContext.current_session_id()
        snapshot = ShoppingContext.current()
        buyer_id = snapshot.buyer_id if snapshot else "anonymous"
        deleted = await store.delete(buyer_id, statement)
        remaining = await store.list_by_buyer(buyer_id)
        if broker:
            broker.publish(session_id, "tool.result", {
                "tool": "forget_preference_tool",
                "deleted": deleted,
                "remaining": [f"[{p.kind}] {p.statement}" for p in remaining],
            })
        return {"deleted": deleted, "remaining": [f"[{p.kind}] {p.statement}" for p in remaining]}

    return [
        StructuredTool.from_function(
            name="remember_preference_tool",
            description="记住买家稳定偏好，跨会话生效；一次性临时要求不要记。",
            args_schema=RememberPreferenceArgs,
            coroutine=remember_preference_tool,
        ),
        StructuredTool.from_function(
            name="forget_preference_tool",
            description="撤回买家历史偏好；statement 必须与现有偏好原文完全一致。",
            args_schema=ForgetPreferenceArgs,
            coroutine=forget_preference_tool,
        ),
    ]
