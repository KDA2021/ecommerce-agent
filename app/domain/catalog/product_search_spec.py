# -*- coding: utf-8 -*-
"""ProductSearchSpec 值对象

SearchAgent 把买家自然语言 query 改写为标准化检索规格：
normalized_query 用于召回，槽位（category / price_band / ship_to / locale）用于过滤。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProductSearchSpec:
    """检索规格值对象：召回 query + 过滤槽位 + 价格硬约束。"""

    normalized_query: str
    category: Optional[str] = None
    ship_to: Optional[str] = None
    locale: str = "zh-CN"
    top_k: int = 5
    # 到手价目标币种：命中 ship_to 时商品卡内联 landed_price（小计+运费+关税）
    target_currency: str = "CNY"
    # 价格硬约束（目标币种主单位）：硬约束由检索链路结构化过滤，不交给 embedding/reranker
    price_max_major: Optional[float] = None
    # 买家长期偏好原文（如"喜欢小众设计"）：仅用于商品卡 reasons 的"偏好记忆命中"判定，
    # 不参与召回/排序。由检索入口从偏好存储读取后注入，模型不需要（也不应该）编造。
    preference_statements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验 query 非空、top_k 为正整数；偏好原文统一收敛成 tuple。"""
        if not self.normalized_query or not self.normalized_query.strip():
            raise ValueError("ProductSearchSpec.normalized_query required")
        if self.top_k <= 0:
            raise ValueError("ProductSearchSpec.top_k 必须为正整数")
        if not isinstance(self.preference_statements, tuple):
            object.__setattr__(self, "preference_statements", tuple(self.preference_statements))

