# -*- coding: utf-8 -*-
"""UserFeedback 值对象 + FeedbackStore 端口（P10-1 反馈闭环）。

产品口径：
- ``score=1``  点赞，``score=-1`` 点踩；
- ``reason`` 只对点踩有意义，取值必须落在固定枚举里，方便 Dashboard 聚合与
  badcase 回流；点赞可以不填原因；
- 前端展示中文 label，后端 / 存储统一使用稳定的英文 code，避免改文案就迁数据。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

#: reason code -> 前端中文 label。
REASON_LABELS: dict[str, str] = {
    "product_irrelevant": "商品不相关",
    "price_mismatch": "价格不符合",
    "too_slow": "回答太慢",
    "wrong_info": "信息有误",
    "other": "其他",
}

#: 合法 reason code 集合。
VALID_REASONS = tuple(REASON_LABELS)

#: 中文 label -> code 反查（API 同时接受 code 与中文 label）。
_LABEL_TO_CODE = {label: code for code, label in REASON_LABELS.items()}

#: 合法点赞 / 点踩取值。
VALID_SCORES = (1, -1)

#: 回复预览最多存多少个字符（避免把整段长文写进反馈表）。
REPLY_PREVIEW_LIMIT = 500


def normalize_reason(value: str | None) -> str:
    """把前端传来的原因归一化成 reason code。

    接受英文 code（``price_mismatch``）与中文 label（``价格不符合``）两种写法；
    空值返回空串；无法识别抛 ``ValueError``，由 API 层转成 422。

    Args:
        value: 原始原因文本。

    Returns:
        归一化后的 reason code 或空串。

    Raises:
        ValueError: 原因不在枚举内。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in REASON_LABELS:
        return raw
    if raw in _LABEL_TO_CODE:
        return _LABEL_TO_CODE[raw]
    raise ValueError(f"reason 必须是 {list(REASON_LABELS)} 之一或对应中文：{raw}")


def reason_label(code: str) -> str:
    """reason code -> 中文 label；未知 code 原样返回。

    Args:
        code: reason code。

    Returns:
        中文 label 或原始 code。
    """
    return REASON_LABELS.get(code, code)


@dataclass(frozen=True)
class UserFeedback:
    """一条用户反馈值对象。"""

    session_id: str
    score: int
    query: str = ""
    trace_id: str = ""
    reply_preview: str = ""
    reason: str = ""
    buyer_id: str = ""
    created_at: str = ""
    id: int = 0

    def __post_init__(self) -> None:
        """校验并归一化字段，缺省时补创建时间。

        Raises:
            ValueError: session_id 为空、score 非法或 reason 非法。
        """
        if not self.session_id or not str(self.session_id).strip():
            raise ValueError("UserFeedback.session_id required")
        if self.score not in VALID_SCORES:
            raise ValueError(f"UserFeedback.score 必须是 {VALID_SCORES}：{self.score}")
        # reason 允许为空（点赞），非空时必须命中枚举；点踩必须给出有效原因。
        normalized = normalize_reason(self.reason)
        if self.score == -1 and not normalized:
            raise ValueError("点踩必须提交有效 reason")
        if normalized != self.reason:
            object.__setattr__(self, "reason", normalized)
        preview = str(self.reply_preview or "")
        if len(preview) > REPLY_PREVIEW_LIMIT:
            object.__setattr__(self, "reply_preview", preview[:REPLY_PREVIEW_LIMIT])
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc).isoformat())

    @property
    def reason_text(self) -> str:
        """返回 reason 的中文 label（无原因时为空串）。"""
        return reason_label(self.reason)

    def to_dict(self) -> dict:
        """序列化成 API 响应 dict。

        Returns:
            含 id / 分数 / 原因 code 与中文 label 的 dict。
        """
        return {
            "id": self.id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "buyer_id": self.buyer_id,
            "query": self.query,
            "reply_preview": self.reply_preview,
            "score": self.score,
            "reason": self.reason,
            "reason_label": self.reason_text,
            "created_at": self.created_at,
        }


class FeedbackStore(ABC):
    """用户反馈持久化端口。"""

    @abstractmethod
    async def add(self, feedback: UserFeedback) -> int:
        """写入一条反馈，返回自增 id。

        Args:
            feedback: 反馈值对象。

        Returns:
            新记录 id。
        """

    @abstractmethod
    async def list_by_session(self, session_id: str, limit: int = 100) -> list[UserFeedback]:
        """按会话 id 查询反馈（时间正序）。

        Args:
            session_id: 会话 id。
            limit: 最多返回条数。

        Returns:
            反馈列表。
        """

    @abstractmethod
    async def list_all(self, limit: int | None = None) -> list[UserFeedback]:
        """查询全部反馈（Dashboard 聚合用）。

        Args:
            limit: 可选条数上限。

        Returns:
            反馈列表。
        """
