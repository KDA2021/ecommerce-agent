# -*- coding: utf-8 -*-
"""BuyerPreference 值对象 + PreferenceStore 端口

买家长期偏好（"上次说不要塑料"）跨会话持久化的领域建模：
    - kind=like     正向偏好（如"喜欢小众设计"）
    - kind=dislike  负向偏好 / 黑名单（如"不要塑料材质"）
Infrastructure 当前提供 MySQL 实现（MySqlPreferenceStore）；端口保留，便于后续替换其它存储。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

VALID_KINDS = ("like", "dislike")


@dataclass(frozen=True)
class BuyerPreference:
    """买家长期偏好值对象：kind 为 like / dislike，statement 为偏好原文。"""

    buyer_id: str
    kind: str  # like / dislike
    statement: str  # 一句话偏好陈述，如"不要塑料材质"
    created_at: str = ""
    attribute: str | None = None
    value: str | None = None
    currency: str | None = None
    source_session_id: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        """校验 kind 与 statement，缺省时补创建时间。"""
        if self.kind not in VALID_KINDS:
            raise ValueError(f"BuyerPreference.kind 必须是 {VALID_KINDS}：{self.kind}")
        if not self.statement or not self.statement.strip():
            raise ValueError("BuyerPreference.statement required")
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc).isoformat())


class PreferenceStore(ABC):
    """买家偏好持久化端口。"""

    @abstractmethod
    async def append(self, preference: BuyerPreference) -> None:
        """追加偏好；同 buyer 同 statement 幂等去重。

        Args:
            preference: 买家偏好。
        """

    @abstractmethod
    async def list_by_buyer(self, buyer_id: str) -> list[BuyerPreference]:
        """按 buyer 列出全部偏好。

        Args:
            buyer_id: 买家 id。

        Returns:
            偏好列表。
        """
        ...

    @abstractmethod
    async def delete(self, buyer_id: str, statement: str) -> bool:
        """按 statement **精确匹配**删除；命中返回 True，未命中返回 False。

        刻意不做模糊/向量匹配：删偏好是不可逆写操作，而“不要塑料”与
        “不要塑料包装”这类语句相似度极高，模糊匹配会误删。调用方在未命中时
        应把现存偏好列表回给模型，让它用原文重试。

        不按 kind 区分：同 statement 的 like 与 dislike 条目会一并清除。

        Args:
            buyer_id: 买家 id。
            statement: 偏好原文。
        """
