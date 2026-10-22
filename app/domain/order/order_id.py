# -*- coding: utf-8 -*-
"""订单号格式与解析（``GBX-000042``）。

订单号是外部可见标识，格式规则属于领域知识，因此放在 domain 层，
由 MySQL / InMemory 两种仓储实现共用，避免“格式化”这件事出现两份口径。

约定：
    - 前缀固定 ``GBX-``；
    - 序号是正整数，至少 6 位零填充（超过 6 位后自然增长，不再截断）；
    - 解析时只认前缀 + 数字，历史脏号（如 ``LEGACY-1``）直接跳过。
"""
from __future__ import annotations

import re
from typing import Iterable

#: 订单号前缀。
ORDER_ID_PREFIX = "GBX-"

#: 序号最小宽度；超过这个宽度时按实际位数输出。
ORDER_ID_SEQUENCE_WIDTH = 6

#: 历史订单号形如 ``GBX-000042``；只解析数字段，跳过多余前后缀。
ORDER_ID_PATTERN = re.compile(rf"{re.escape(ORDER_ID_PREFIX)}(\d+)")


def format_order_id(sequence: int) -> str:
    """序号 → 订单号。

    Args:
        sequence: 正整数序号。

    Returns:
        订单号字符串，如 ``GBX-000042``。
    """
    return f"{ORDER_ID_PREFIX}{sequence:0{ORDER_ID_SEQUENCE_WIDTH}d}"


def parse_order_sequence(order_id: str) -> int | None:
    """订单号 → 序号；无法解析时返回 None。

    Args:
        order_id: 订单号字符串。

    Returns:
        序号；非 ``GBX-数字`` 形式返回 None。
    """
    match = ORDER_ID_PATTERN.search(order_id or "")
    return int(match.group(1)) if match else None


def max_order_sequence(order_ids: Iterable[str]) -> int:
    """取一组订单号里的最大序号（跳过无法解析的历史脏号）。

    Args:
        order_ids: 已有订单号集合。

    Returns:
        最大序号；没有任何可解析订单号时返回 0。
    """
    sequences = [sequence for order_id in order_ids if (sequence := parse_order_sequence(order_id)) is not None]
    return max(sequences, default=0)
