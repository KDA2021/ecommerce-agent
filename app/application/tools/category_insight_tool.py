# -*- coding: utf-8 -*-
"""category_insight_tool：把品类知识库检索包装成 LangChain 工具。"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.infrastructure.context import ShoppingContext


class CategoryInsightArgs(BaseModel):
    """category_insight_tool 的结构化入参。"""

    question: str = Field(description="选购常识问题，建议带品类词")
    top_k: int = Field(default=3, description="返回知识片段数量")


def build_category_insight_tool(kb, broker=None):
    """把品类知识库检索包装成 LangChain 工具。

    Args:
        kb: 具备 ``async search(question, top_k)`` 的知识库。
        broker: 可选事件总线。

    Returns:
        ``StructuredTool``。
    """
    async def category_insight_tool(question: str, top_k: int = 3) -> dict:
        """查询品类选购常识并返回 ``{'insights': [...]}``。

        Args:
            question: 选购常识问题。
            top_k: 返回片段数。

        Returns:
            ``{'insights': [...]}``，知识库不可用时为 ``{'error': ...}``。
        """
        session_id = ShoppingContext.current_session_id()
        if broker:
            broker.publish(session_id, "tool.invoke", {"tool": "category_insight_tool", "args": {"question": question, "top_k": top_k}})
        try:
            insights = await kb.search(question, top_k=top_k)
        except Exception as err:
            if broker:
                broker.publish(session_id, "tool.result", {"tool": "category_insight_tool", "error": str(err)})
            return {"error": f"品类知识库不可用：{err}"}
        if broker:
            broker.publish(session_id, "tool.result", {"tool": "category_insight_tool", "hit_count": len(insights)})
        return {"insights": insights}

    return StructuredTool.from_function(
        name="category_insight_tool",
        description="查询品类选购常识、价格区间与避坑点；需要具体商品清单时用 product_search。",
        args_schema=CategoryInsightArgs,
        coroutine=category_insight_tool,
    )
