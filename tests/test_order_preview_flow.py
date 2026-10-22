# -*- coding: utf-8 -*-
"""P10-2 下单确认与证据展示：前端纯函数 + 后端工具契约 + 真实门控回归。

四层覆盖：
1. **前端纯函数**：以子进程跑 `frontend/tests/order-preview.test.mts`
   （Node 原生 test runner + 原生 TS 类型剥离，不引入 vitest / ts-node 依赖），
   覆盖 order.preview 解析、确认/取消分支、验证徽章映射；
2. **后端工具契约**：锁死 create_order / query_order / cancel_order 的入参
   Pydantic 模型与 build_order_tools 签名（旧调用方仍可不传确认参数）；
3. **下单确认管理器**：confirm / cancel / timeout 三态、未知 token、幂等重复
   resolve；
4. **真实门控**：create_order 收到确认才落库；cancel / timeout 时
   PlaceOrderUseCase 完全不被触碰（内存仓储断言订单数为 0）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.application.tools.order_tools import build_order_tools
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
NODE_TEST_FILE = "tests/order-preview.test.mts"

#: 前端 orderPreview.ts 解析 create_order 入参时依赖的字段。
FRONTEND_ITEM_KEYS = {"product_id", "sku_id", "quantity"}
#: 后端 create_order 结果快照里前端必须能解析到的字段。
FRONTEND_ORDER_KEYS = {"order_id", "status", "verification_status"}
#: 订单快照行项目里前端金额列依赖的字段。
FRONTEND_LINE_KEYS = {"title", "unit_price_major", "quantity"}


# --------------------------------------------------------------------------- #
# 1. 前端纯函数（Node 原生 test runner）
# --------------------------------------------------------------------------- #
def _node_executable() -> str | None:
    """返回可用的 node 可执行文件；缺失返回 None（CI 无 Node 时跳过）。"""
    return shutil.which("node")


def test_frontend_pure_functions_pass_in_node_test_runner():
    """跑 frontend/tests/order-preview.test.mts，覆盖预览/确认/取消/徽章。"""
    node = _node_executable()
    if node is None:
        pytest.skip("未安装 Node，跳过前端纯函数单测")
    if not (FRONTEND / NODE_TEST_FILE).exists():
        pytest.fail(f"前端测试文件缺失：{NODE_TEST_FILE}")

    proc = subprocess.run(
        [node, "--test", NODE_TEST_FILE],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, f"node --test 失败（exit={proc.returncode}）：\n{output}"
    # 防止测试文件被清空后“0 test passed”也返回 0。
    assert "pass " in output, output
    assert "fail 0" in output, output


def test_frontend_helper_source_keeps_contract_fields():
    """前端 helper 源码必须仍引用后端契约字段（字段漂移回归）。"""
    source = (FRONTEND / "src" / "orderPreview.ts").read_text(encoding="utf-8")
    for key in sorted(FRONTEND_ITEM_KEYS | FRONTEND_ORDER_KEYS | FRONTEND_LINE_KEYS):
        assert key in source, f"orderPreview.ts 缺少契约字段：{key}"
    for tool in ("create_order", "query_order", "cancel_order"):
        assert tool in source, f"orderPreview.ts 缺少工具名：{tool}"
    # 先预览后下单：确认 / 取消两个分支出现在同一状态机里。
    assert "confirm" in source and "cancel" in source
    assert "VERIFIED" in source and "UNKNOWN" in source
    # Task 2 修复主路径：消费 order.preview 真实握手，并把 token 发回确认接口。
    assert "parseOrderPreview" in source and "buildPreviewFromOrderPreview" in source
    assert "confirm_order" in source
    api_source = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    assert "/api/commerce/orders/confirm" in api_source
    assert "confirmOrder" in api_source
    app_source = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'addEventListener("order.preview"' in app_source
    assert "confirmOrder(" in app_source


# --------------------------------------------------------------------------- #
# 2. 后端工具契约（本任务明确要求不变）
# --------------------------------------------------------------------------- #
def test_order_tool_argument_models_unchanged():
    """create_order / query_order / cancel_order 入参模型字段集合锁定。"""
    from app.application.tools.order_tools import (
        AddressArgs,
        CancelOrderArgs,
        CreateOrderArgs,
        OrderItemArgs,
        QueryOrderArgs,
    )

    assert set(OrderItemArgs.model_fields) == FRONTEND_ITEM_KEYS
    assert set(CreateOrderArgs.model_fields) == {"items", "shipping_address"}
    assert {"recipient_name", "country", "city", "address_line"} <= set(AddressArgs.model_fields)
    assert set(QueryOrderArgs.model_fields) == {"order_id"}
    assert set(CancelOrderArgs.model_fields) == {"order_id", "reason"}
    # 可选字段不能从「有默认值」退化成「必填」，否则前端预览会被迫补默认值。
    for model, optional in (
        (OrderItemArgs, {"quantity"}),
        (AddressArgs, {"state", "postal_code", "phone"}),
    ):
        for field in optional:
            assert not model.model_fields[field].is_required(), f"{model.__name__}.{field} 变成必填"


def test_build_order_tools_signature_unchanged():
    """build_order_tools 位置参数锁定，确认参数必须是可选关键字参数（向后兼容）。"""
    from app.application.tools.order_tools import build_order_tools

    params = inspect.signature(build_order_tools).parameters
    # 旧调用方按位置传参的顺序不能变。
    assert list(params)[:7] == [
        "place",
        "query",
        "cancel",
        "broker",
        "task_store",
        "task_id_prefix",
        "redis_cache",
    ]
    assert params["place"].default is inspect.Parameter.empty
    assert params["query"].default is inspect.Parameter.empty
    assert params["cancel"].default is inspect.Parameter.empty
    assert params["broker"].default is None
    assert params["task_store"].default is None
    assert params["task_id_prefix"].default == "order"
    # P10-2 修复新增：真实确认管理器与 settings 都是可选关键字参数、默认 None。
    for name in ("confirmation_manager", "settings"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is None, name


def test_order_tool_names_unchanged():
    """工具名仍是 create_order / query_order / cancel_order。"""
    import asyncio

    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )

    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    tools = build_order_tools(
        PlaceOrderUseCase(product_repo, order_repo),
        QueryOrderUseCase(order_repo),
        CancelOrderUseCase(product_repo, order_repo),
    )
    assert [tool.name for tool in tools] == ["create_order", "query_order", "cancel_order"]
    assert asyncio.iscoroutinefunction(tools[0].coroutine)


# --------------------------------------------------------------------------- #
# 3. 事件字段契约（真实跑工具，断言前端依赖字段存在）
# --------------------------------------------------------------------------- #
class _CapturingBroker:
    """记录 publish 调用的假 broker（补 trace_id，对齐 EventBroker 行为）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, session_id: str, event_type: str, payload: dict) -> None:
        from app.infrastructure.context import ShoppingContext

        if "trace_id" not in payload:
            trace_id = ShoppingContext.current_trace_id()
            if trace_id:
                payload = {**payload, "trace_id": trace_id}
        self.events.append((session_id, event_type, payload))

    def by_type(self, event_type: str) -> list[dict]:
        return [payload for _, kind, payload in self.events if kind == event_type]


def _order_tools():
    """装配一套内存版订单工具。"""
    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )

    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    place = PlaceOrderUseCase(product_repo, order_repo)
    query = QueryOrderUseCase(order_repo)
    cancel = CancelOrderUseCase(product_repo, order_repo)
    return build_order_tools(place, query, cancel), order_repo


_SHIPPING_ADDRESS = {
    "recipient_name": "张三",
    "country": "CN",
    "state": "浙江",
    "city": "杭州",
    "address_line": "西湖区某路 1 号",
    "postal_code": "310000",
    "phone": "13800000000",
}


@pytest.mark.asyncio
async def test_create_order_result_keeps_frontend_contract_fields():
    """create_order 结果必须带 order_id / status / lines.unit_price_major 等前端字段。"""
    from app.application.evidence.task_state import InMemoryTaskStateStore
    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )

    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    tools = build_order_tools(
        PlaceOrderUseCase(product_repo, order_repo),
        QueryOrderUseCase(order_repo),
        CancelOrderUseCase(product_repo, order_repo),
        task_store=InMemoryTaskStateStore(),
    )
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-preview",
            buyer_id="buyer-preview",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-preview",
        )
    )
    try:
        result = await create.ainvoke(
            {
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 2}],
                "shipping_address": _SHIPPING_ADDRESS,
            }
        )
    finally:
        ShoppingContext.reset(token)

    assert not result.get("error"), result
    # 前端 orderPreview.ts 的 extractEvidence / buildResultCard 读取这些字段。
    assert FRONTEND_ORDER_KEYS <= set(result)
    assert result["status"] == "CONFIRMED"
    assert result["verification_status"] in {"VERIFIED", "EXECUTED", "UNVERIFIED"}
    assert result["order_id"].startswith("GBX-")
    assert isinstance(result["total_amount_major"], (int, float))

    first_line = result["lines"][0]
    assert FRONTEND_LINE_KEYS <= set(first_line)
    assert first_line["quantity"] == 2
    assert first_line["unit_price_major"] > 0
    # 行项目小计 × 数量应等于订单总额（前端合计口径一致）。
    assert first_line["unit_price_major"] * first_line["quantity"] == result["total_amount_major"]


@pytest.mark.asyncio
async def test_tool_invoke_event_payload_matches_frontend_parser():
    """tool.invoke(create_order) 载荷必须是前端 parseCreateOrderInvocation 认识的结构。"""
    broker = _CapturingBroker()
    from app.application.evidence.task_state import InMemoryTaskStateStore
    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )

    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    tools = build_order_tools(
        PlaceOrderUseCase(product_repo, order_repo),
        QueryOrderUseCase(order_repo),
        CancelOrderUseCase(product_repo, order_repo),
        broker=broker,
        task_store=InMemoryTaskStateStore(),
    )
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-events",
            buyer_id="buyer-events",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-events",
        )
    )
    try:
        await create.ainvoke(
            {
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                "shipping_address": _SHIPPING_ADDRESS,
            }
        )
    finally:
        ShoppingContext.reset(token)

    invoke_payloads = broker.by_type("tool.invoke")
    assert len(invoke_payloads) == 1
    invoke = invoke_payloads[0]
    assert invoke["tool"] == "create_order"
    assert set(invoke["args"]) == {"items", "shipping_address"}
    assert FRONTEND_ITEM_KEYS <= set(invoke["args"]["items"][0])
    assert invoke["args"]["shipping_address"]["city"] == "杭州"
    # 前端按 trace_id 关联本轮 invoke / result。
    assert invoke["trace_id"] == "trace-events"

    result_payloads = broker.by_type("tool.result")
    assert len(result_payloads) == 1
    result = result_payloads[0]
    assert result["tool"] == "create_order"
    assert FRONTEND_ORDER_KEYS <= set(result["order"])
    assert result["order"]["status"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_cancel_order_returns_snapshot_for_result_badge():
    """取消走 cancel_order，可被前端结果卡片解析为 CANCELLED + 原因。"""
    tools, _ = _order_tools()
    create = next(tool for tool in tools if tool.name == "create_order")
    cancel = next(tool for tool in tools if tool.name == "cancel_order")

    created = await create.ainvoke(
        {
            "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
            "shipping_address": _SHIPPING_ADDRESS,
        }
    )
    cancelled = await cancel.ainvoke({"order_id": created["order_id"], "reason": "用户取消"})

    assert not cancelled.get("error"), cancelled
    assert cancelled["order_id"] == created["order_id"]
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancel_reason"] == "用户取消"
    assert "shipping_address" in cancelled and "lines" in cancelled


@pytest.mark.asyncio
async def test_query_order_returns_snapshot_usable_as_verification_probe():
    """UNKNOWN「结果待确认」时，前端用 query_order/GET 订单作为核对入口。"""
    tools, _ = _order_tools()
    create = next(tool for tool in tools if tool.name == "create_order")
    query = next(tool for tool in tools if tool.name == "query_order")

    created = await create.ainvoke(
        {
            "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
            "shipping_address": _SHIPPING_ADDRESS,
        }
    )
    fetched = await query.ainvoke({"order_id": created["order_id"]})

    assert not fetched.get("error"), fetched
    assert fetched["order_id"] == created["order_id"]
    assert fetched["status"] == "CONFIRMED"
    assert FRONTEND_ORDER_KEYS - {"verification_status"} <= set(fetched)
    # query_order 不快照证据状态，前端据此展示 UNKNOWN「结果待确认」并给查询入口。
    assert "verification_status" not in fetched


# --------------------------------------------------------------------------- #
# 4. 下单确认管理器（Task 2 修复核心）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_confirmation_manager_confirm_then_wait_returns_confirmed():
    """create_preview → resolve(confirm) → wait 立即返回 confirmed，载荷可按 token 取回。"""
    from app.application.order_confirmation import OrderConfirmationManager

    manager = OrderConfirmationManager()
    token = await manager.create_preview({"items": [{"sku_id": "P1001-S1"}]})

    async def _resolve() -> str:
        return await manager.resolve(token, "confirm")

    assert await asyncio.gather(manager.wait(token, 5.0), _resolve()) == [
        "confirmed",
        "confirmed",
    ]
    assert await manager.status(token) == "confirmed"
    payload = await manager.preview_payload(token)
    assert payload is not None and payload["items"][0]["sku_id"] == "P1001-S1"


@pytest.mark.asyncio
async def test_confirmation_manager_cancel_outcome():
    """resolve(cancel) → wait 返回 cancelled，绝不等同于 confirmed。"""
    from app.application.order_confirmation import OrderConfirmationManager

    manager = OrderConfirmationManager()
    token = await manager.create_preview({"items": []})
    assert await manager.resolve(token, "cancel") == "cancelled"
    assert await manager.wait(token, 5.0) == "cancelled"


@pytest.mark.asyncio
async def test_confirmation_manager_timeout_returns_timeout():
    """无人确认时 wait 超时返回 timeout，不泄露旧决议。"""
    from app.application.order_confirmation import OrderConfirmationManager

    manager = OrderConfirmationManager()
    token = await manager.create_preview({"items": []})
    assert await manager.wait(token, 0.02) == "timeout"
    # 超时后条目被清理，避免 token 表无界增长。
    assert await manager.status(token) == "unknown"
    assert await manager.preview_payload(token) is None


@pytest.mark.asyncio
async def test_confirmation_manager_unknown_token_and_idempotent_resolve():
    """未知 token 返回 unknown；重复 resolve 不覆盖第一次决议。"""
    from app.application.order_confirmation import OrderConfirmationManager

    manager = OrderConfirmationManager()
    assert await manager.resolve("no-such-token", "confirm") == "unknown"
    assert await manager.wait("no-such-token", 0.02) == "timeout"

    token = await manager.create_preview({"items": []})
    assert await manager.resolve(token, "confirm") == "confirmed"
    # 手抖重复点取消不能把已确认改回取消。
    assert await manager.resolve(token, "cancel") == "confirmed"


@pytest.mark.asyncio
async def test_confirmation_manager_rejects_illegal_action():
    """非法 action 抛 ValueError（REST 层转 422），不改动 token 状态。"""
    from app.application.order_confirmation import OrderConfirmationManager

    manager = OrderConfirmationManager()
    token = await manager.create_preview({"items": []})
    with pytest.raises(ValueError):
        await manager.resolve(token, "delete")
    assert await manager.status(token) == "pending"


# --------------------------------------------------------------------------- #
# 5. 真实门控：确认才落库，取消 / 超时绝不调用 place
# --------------------------------------------------------------------------- #
class _TimeoutSettings:
    """最小 Settings 替身：只暴露 create_order 需要的确认超时。"""

    order_confirm_timeout_seconds = 0.05


def _gated_order_tools(*, broker=None, confirm_timeout: float = 5.0):
    """装配一套带真实确认管理器的内存版订单工具。

    Args:
        broker: 可选事件总线。
        confirm_timeout: 传入工具的确认等待秒数。

    Returns:
        ``(tools, order_repo, confirmation_manager)``。
    """
    from app.application.order_confirmation import OrderConfirmationManager
    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )

    class _Settings:
        order_confirm_timeout_seconds = confirm_timeout

    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    manager = OrderConfirmationManager()
    tools = build_order_tools(
        PlaceOrderUseCase(product_repo, order_repo),
        QueryOrderUseCase(order_repo),
        CancelOrderUseCase(product_repo, order_repo),
        broker=broker,
        confirmation_manager=manager,
        settings=_Settings(),
    )
    return tools, order_repo, manager


async def _wait_for_pending_token(manager, *, attempts: int = 200) -> str:
    """轮询等待 create_order 发布预览并登记 token。

    Args:
        manager: 确认管理器。
        attempts: 最多轮询次数（每次让出事件循环）。

    Returns:
        第一个 pending token。

    Raises:
        AssertionError: 超时仍没有 pending token。
    """
    for _ in range(attempts):
        pending = await manager.pending_tokens()
        if pending:
            return pending[0]
        await asyncio.sleep(0.005)
    raise AssertionError("create_order 未在规定时间内创建确认 token")


def _create_args(quantity: int = 1) -> dict:
    """create_order 调用入参。

    Args:
        quantity: 商品数量。

    Returns:
        工具入参 dict。
    """
    return {
        "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": quantity}],
        "shipping_address": _SHIPPING_ADDRESS,
    }


@pytest.mark.asyncio
async def test_order_preview_event_carries_token_and_price_free_preview():
    """order.preview 必须在 place 之前发出：token + preview，且 preview 不含单价。"""
    broker = _CapturingBroker()
    tools, _order_repo, manager = _gated_order_tools(broker=broker)
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-gate-event",
            buyer_id="buyer-gate",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-gate-event",
        )
    )
    task = asyncio.create_task(create.ainvoke(_create_args()))
    try:
        confirm_token = await _wait_for_pending_token(manager)
        payloads = broker.by_type("order.preview")
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["tool"] == "create_order"
        assert payload["token"] == confirm_token
        assert payload["trace_id"] == "trace-gate-event"
        assert set(payload["preview"]) == {"items", "shipping_address"}
        preview_item = payload["preview"]["items"][0]
        assert FRONTEND_ITEM_KEYS <= set(preview_item)
        assert "unit_price_major" not in preview_item
        assert "unit_price" not in preview_item
        # 事件顺序：tool.invoke → order.preview（挂起前），尚无 tool.result。
        assert [kind for _, kind, _ in broker.events] == ["tool.invoke", "order.preview"]
        # 还没确认：place 未被调用，仓库里没有订单。
        assert len(_order_repo._orders) == 0

        assert await manager.resolve(confirm_token, "confirm") == "confirmed"
        result = await task
    finally:
        if not task.done():
            task.cancel()
        ShoppingContext.reset(token)

    assert not result.get("cancelled"), result
    assert result["order_id"].startswith("GBX-")
    assert len(_order_repo._orders) == 1
    assert [kind for _, kind, _ in broker.events] == [
        "tool.invoke",
        "order.preview",
        "tool.result",
    ]


@pytest.mark.asyncio
async def test_create_order_cancel_never_calls_place():
    """取消确认 → 返回 cancelled 结果，PlaceOrderUseCase 完全不被触碰。"""
    broker = _CapturingBroker()
    tools, order_repo, manager = _gated_order_tools(broker=broker)
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-gate-cancel",
            buyer_id="buyer-gate",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-gate-cancel",
        )
    )
    task = asyncio.create_task(create.ainvoke(_create_args()))
    try:
        confirm_token = await _wait_for_pending_token(manager)
        assert await manager.resolve(confirm_token, "cancel") == "cancelled"
        result = await task
    finally:
        if not task.done():
            task.cancel()
        ShoppingContext.reset(token)

    assert result == {"cancelled": True, "reason": "user_cancelled", "order": None}
    assert len(order_repo._orders) == 0
    result_payloads = broker.by_type("tool.result")
    assert result_payloads[-1]["tool"] == "create_order"
    assert result_payloads[-1]["order"] == result


@pytest.mark.asyncio
async def test_create_order_timeout_never_calls_place():
    """确认超时 → 返回 confirmation_timeout，且不落库。"""
    broker = _CapturingBroker()
    tools, order_repo, _manager = _gated_order_tools(broker=broker, confirm_timeout=0.05)
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-gate-timeout",
            buyer_id="buyer-gate",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-gate-timeout",
        )
    )
    try:
        result = await asyncio.wait_for(create.ainvoke(_create_args()), timeout=5.0)
    finally:
        ShoppingContext.reset(token)

    assert result == {"cancelled": True, "reason": "confirmation_timeout", "order": None}
    assert len(order_repo._orders) == 0
    assert [kind for _, kind, _ in broker.events] == [
        "tool.invoke",
        "order.preview",
        "tool.result",
    ]


@pytest.mark.asyncio
async def test_confirm_manager_resolves_token_within_confirm_timeout():
    """确认发生在超时之前：place 执行且订单真实落库。"""
    tools, order_repo, manager = _gated_order_tools(confirm_timeout=5.0)
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-gate-confirm",
            buyer_id="buyer-gate",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-gate-confirm",
        )
    )
    task = asyncio.create_task(create.ainvoke(_create_args(quantity=2)))
    try:
        confirm_token = await _wait_for_pending_token(manager)
        await manager.resolve(confirm_token, "confirm")
        result = await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()
        ShoppingContext.reset(token)

    assert not result.get("cancelled"), result
    assert result["status"] == "CONFIRMED"
    assert result["lines"][0]["quantity"] == 2
    assert len(order_repo._orders) == 1


@pytest.mark.asyncio
async def test_cancelled_create_order_clears_pending_token():
    """请求被取消（客户端断开 / 工具超时）：清理 pending token，且不落库。"""
    tools, order_repo, manager = _gated_order_tools(confirm_timeout=30.0)
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-gate-abort",
            buyer_id="buyer-gate",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-gate-abort",
        )
    )
    task = asyncio.create_task(create.ainvoke(_create_args()))
    try:
        confirm_token = await _wait_for_pending_token(manager)
        assert await manager.status(confirm_token) == "pending"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        ShoppingContext.reset(token)

    assert await manager.status(confirm_token) == "unknown"
    assert await manager.pending_tokens() == []
    assert len(order_repo._orders) == 0


@pytest.mark.asyncio
async def test_create_order_without_manager_keeps_direct_execution():
    """不传 confirmation_manager 时保持旧行为：直接下单、不发 order.preview。"""
    broker = _CapturingBroker()
    tools = build_order_tools(
        *_use_cases(),
        broker=broker,
    )
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-no-manager",
            buyer_id="buyer-gate",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-no-manager",
        )
    )
    try:
        result = await create.ainvoke(_create_args())
    finally:
        ShoppingContext.reset(token)

    assert not result.get("cancelled") and not result.get("error"), result
    assert result["status"] == "CONFIRMED"
    assert broker.by_type("order.preview") == []
    assert broker.by_type("tool.result")[-1]["order"]["order_id"] == result["order_id"]


def _use_cases():
    """构造内存版 (place, query, cancel) 用例三元组。"""
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )

    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    return (
        PlaceOrderUseCase(product_repo, order_repo),
        QueryOrderUseCase(order_repo),
        CancelOrderUseCase(product_repo, order_repo),
    )


def test_documented_frontend_contract_is_json_serializable():
    """契约字段名可 JSON 序列化，保证 SSE 线上载荷与测试常量一致。"""
    contract = {
        "invoke": {"tool": "create_order", "args": {"items": sorted(FRONTEND_ITEM_KEYS)}},
        "result": sorted(FRONTEND_ORDER_KEYS),
        "lines": sorted(FRONTEND_LINE_KEYS),
    }
    assert json.loads(json.dumps(contract)) == contract
