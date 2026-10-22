# -*- coding: utf-8 -*-
"""product_search 工具：结构化参数 → CatalogSearchUseCase → dict。"""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.context import ShoppingContext


def _publish(broker, event_type: str, payload: dict) -> None:
    """向 broker 发布事件（broker 为空则跳过）。

    Args:
        broker: 事件总线。
        event_type: 事件类型。
        payload: 事件载荷。
    """
    if broker is not None:
        broker.publish(ShoppingContext.current_session_id(), event_type, payload)


class ProductSearchArgs(BaseModel):
    """product_search 的结构化入参。"""

    normalized_query: str = Field(description="标准化检索词，保留品类词与关键属性词")
    category: str | None = Field(default=None, description="品类槽位，可选")
    ship_to: str | None = Field(default=None, description="收货国家二位码，可选，如 CN、US")
    top_k: int = Field(default=5, description="返回候选数量，默认 5")
    price_max_major: float | None = Field(default=None, description="价格上限（主单位），硬约束")
    target_currency: str = Field(default="CNY", description="价格口径币种，默认 CNY")


def build_product_search_tool(usecase: CatalogSearchUseCase, broker=None) -> StructuredTool:
    """把 CatalogSearchUseCase 包装成 product_search 工具。

    Args:
        usecase: 商品检索用例。
        broker: 可选事件总线。

    Returns:
        ``StructuredTool``。
    """
    async def product_search(**kwargs) -> dict:
        """执行商品检索并发布 ``tool.invoke`` / ``tool.result``。

        Args:
            **kwargs: ProductSearchArgs 的字段。

        Returns:
            检索结果 dict，参数非法时为 ``{'error': ...}``。
        """
        _publish(broker, "tool.invoke", {"tool": "product_search", "args": kwargs})
        try:
            spec = ProductSearchSpec(**kwargs)
            result = await usecase.execute(spec)
        except ValueError as err:
            _publish(
                broker,
                "tool.result",
                {"tool": "product_search", "error": str(err)},
            )
            return {"error": str(err)}
        _publish(
            broker,
            "tool.result",
            {
                "tool": "product_search",
                "hit_count": len(result.get("hits", [])),
                "recall_strategy": result.get("recall_strategy"),
                "hits": result.get("hits", []),
            },
        )
        return result

    return StructuredTool.from_function(
        name="product_search",
        description=(
            "检索跨境商品库，返回 Top-K 商品卡 JSON；传 ship_to 时内联到手价；"
            "价格上限由本工具结构化过滤。"
            "每张卡的 reasons 是检索链路算好的事实理由（budget_match 预算命中 / "
            "ship_to_match 可发国家 / preference_match 买家偏好命中），只可如实转述，"
            "不要自己加理由；top_picks 是前 3 名对比行，可直接用于对比展示。"
        ),
        args_schema=ProductSearchArgs,
        coroutine=product_search,
    )
