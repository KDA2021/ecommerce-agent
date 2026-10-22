# -*- coding: utf-8 -*-
"""P14-T3：``_call_place`` 幂等键兼容分支的单元测试。

背景：`build_order_tools` 曾经用 ``inspect.signature`` 探测 ``place.execute`` 是否
支持 ``idempotency_key``。P14-T3 移除该 hack 后，改用「第一次真实调用 + 只对
`unexpected keyword argument` 降级重试」：

1. 生产用例（带幂等键）→ 幂等键必须照旧透传，且只调用一次；
2. 旧用例 / 测试替身（三参旧签名）→ 去掉幂等键重试一次，仍然成功；
3. 用例内部抛出的 ``TypeError``（与签名无关）→ 原样抛出，绝不重试 / 不吞异常。

这三点是「不做签名探测」也要保住的行为契约，故单独覆盖。
"""
from __future__ import annotations

import pytest

from app.application.tools.order_tools import _call_place

_PLACE_KWARGS = {"buyer_id": "buyer-1", "items": [], "shipping_address": None}


class _ModernPlace:
    """P9-3 之后的用例签名：接受并透传 ``idempotency_key``。"""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def execute(self, buyer_id, items, shipping_address, idempotency_key=None):  # noqa: ANN001
        self.calls.append(idempotency_key)
        return {"order_id": "GBX-000001", "status": "CONFIRMED"}


class _LegacyPlace:
    """旧调用方 / 测试替身：只有三个参数，不认识幂等键。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, buyer_id, items, shipping_address):  # noqa: ANN001
        self.calls += 1
        return {"order_id": "GBX-000002", "status": "CONFIRMED"}


class _InnerTypeErrorPlace:
    """签名正确，但函数体内部抛 ``TypeError``（不能被当成签名不兼容）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, buyer_id, items, shipping_address, idempotency_key=None):  # noqa: ANN001
        self.calls += 1
        raise TypeError("内部参数错误，不是签名不兼容")


async def test_call_place_passes_idempotency_key_to_modern_usecase():
    """生产路径：幂等键透传，且只调用一次（不做任何探测性调用）。"""
    place = _ModernPlace()

    result = await _call_place(place, _PLACE_KWARGS, "idem-key-1")

    assert result == {"order_id": "GBX-000001", "status": "CONFIRMED"}
    assert place.calls == ["idem-key-1"]


async def test_call_place_retries_without_key_for_legacy_usecase():
    """兼容路径：旧三参用例去掉幂等键后重试一次，仍然成功。"""
    place = _LegacyPlace()

    result = await _call_place(place, _PLACE_KWARGS, "idem-key-2")

    assert result == {"order_id": "GBX-000002", "status": "CONFIRMED"}
    assert place.calls == 1


async def test_call_place_does_not_mask_internal_type_error():
    """用例内部的 ``TypeError`` 原样抛出：不重试、不降级成功。"""
    place = _InnerTypeErrorPlace()

    with pytest.raises(TypeError, match="内部参数错误"):
        await _call_place(place, _PLACE_KWARGS, "idem-key-3")

    assert place.calls == 1
