# -*- coding: utf-8 -*-
"""最终回复完成声明门控。

模型有时会把“准备下单”说成“已完成下单”。服务层必须在回复发出前，用当前
trace 下的 VERIFIED 任务证据做最后一道校验：没有证据就替换为安全话术，
避免用户以为写操作已经落库而重复操作。
"""
from __future__ import annotations

import re

SAFE_COMPLETION_TEXT = "操作已提交，结果待确认；请勿重复操作"

# 中英文“完成声明”正则；命中且当前 trace 无 VERIFIED 订单时，回复会被替换为安全话术。
_COMPLETION_CLAIM_PATTERNS = (
    re.compile(r"已(?:经)?\s*(?:完成|成功|下单|创建订单?|购买|支付|取消)"),
    re.compile(r"(?:下单|订单|购买|支付|取消)[^。！？\n]{0,12}(?:成功|完成|已创建|已提交)"),
    re.compile(r"successfully\s+(?:placed|created|ordered|confirmed|cancelled|completed)"),
    re.compile(r"\border\s+(?:has\s+been\s+|was\s+)?(?:placed|created|confirmed|cancelled|completed)\b"),
)


class CompletionGuard:
    """基于 VERIFIED 订单证据的完成声明门控。"""

    @staticmethod
    def has_completion_claim(reply: str) -> bool:
        """判断文本是否包含写操作完成声明。

        Args:
            reply: 待检查回复。

        Returns:
            是否包含完成声明。
        """
        text = str(reply or "")
        return any(pattern.search(text) for pattern in _COMPLETION_CLAIM_PATTERNS)

    @staticmethod
    def guard(reply: str, verified_order_ids: list[str]) -> str:
        """没有 VERIFIED 订单证据时，拦截完成声明并替换为安全话术。

        Args:
            reply: Agent 最终回复。
            verified_order_ids: 当前 trace 下已验证存在的订单号列表。

        Returns:
            原回复或安全话术。
        """
        if verified_order_ids:
            return reply
        if CompletionGuard.has_completion_claim(reply):
            return SAFE_COMPLETION_TEXT
        return reply
