# -*- coding: utf-8 -*-
"""preference 节点：读取买家长期偏好并按相关性挑选，写入 ``preference_hint``。"""
from __future__ import annotations

from app.application.agents.state import MainState
from app.infrastructure.context import ShoppingContext


def make_preference_node(preference_store=None, preference_top_k: int = 5):
    """构造偏好注入节点；偏好存储与 top_k 显式传入。

    Args:
        preference_store: 偏好存储，可为 None（此时不注入偏好）。
        preference_top_k: 注入的 like 偏好条数上限。

    Returns:
        ``async def preference_node(state) -> dict``。
    """
    from app.application.memory.preference_selector import (
        PreferenceSelector,
        render_preference_hint,
    )

    selector = PreferenceSelector()

    async def preference_node(state: MainState) -> dict:
        """读取买家长期偏好并按相关性挑选，写入 ``preference_hint``。

        Args:
            state: 父图 state。

        Returns:
            ``{'preference_hint': str}`` 局部更新。
        """
        if preference_store is None:
            return {"preference_hint": ""}
        snapshot = ShoppingContext.current()
        buyer_id = snapshot.buyer_id if snapshot else ""
        if not buyer_id:
            return {"preference_hint": ""}
        preferences = await preference_store.list_by_buyer(buyer_id)
        selected = await selector.select(
            preferences,
            query=str(state["messages"][-1].content),
            top_k=preference_top_k,
        )
        return {"preference_hint": render_preference_hint(selected) if selected else ""}

    return preference_node
