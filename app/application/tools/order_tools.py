# -*- coding: utf-8 -*-
"""订单三工具：create_order / query_order / cancel_order。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.application.action_authorization import action_authorization
from app.application.evidence.task_state import TaskRecord, TaskStateStore, TaskStatus
from app.application.order_confirmation import OrderConfirmationManager
from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    OrderItemInput,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.domain.order.address import Address
from app.infrastructure.context import ShoppingContext

#: 下单确认握手事件类型：create_order 挂起前发布的预览（前端据此渲染确认卡片）。
ORDER_PREVIEW_EVENT = "order.preview"

#: 未配置 Settings 时的确认等待兜底秒数。
DEFAULT_ORDER_CONFIRM_TIMEOUT = 120.0


class OrderItemArgs(BaseModel):
    """下单行参数。"""

    product_id: str = Field(description="商品 ID，如 P1001")
    sku_id: str = Field(description="SKU ID，如 P1001-S1")
    quantity: int = Field(default=1, description="数量，正整数")


class AddressArgs(BaseModel):
    """收货地址参数。"""

    recipient_name: str                                            # 收件人姓名
    country: str = Field(description="国家二位码，如 CN")
    state: str = ""                                                # 省 / 州，可选
    city: str                                                      # 城市
    address_line: str                                              # 详细地址
    postal_code: str = ""                                          # 邮编，可选
    phone: str = ""                                                # 联系电话，可选


class CreateOrderArgs(BaseModel):
    """create_order 入参。"""

    items: list[OrderItemArgs] = Field(description="订单行列表")
    shipping_address: AddressArgs = Field(description="收货地址")


class QueryOrderArgs(BaseModel):
    """query_order 入参。"""

    order_id: str = Field(description="订单号，如 GBX-000001")


class CancelOrderArgs(BaseModel):
    """cancel_order 入参。"""

    order_id: str = Field(description="订单号，如 GBX-000001")
    reason: str = Field(description="取消原因，必填")


def _buyer_id() -> str:
    """从 ShoppingContext 读取真实 buyer_id，缺上下文时回退 anonymous。

    Returns:
        buyer_id。
    """
    snapshot = ShoppingContext.current()
    return snapshot.buyer_id if snapshot else "anonymous"


async def _query_owned(query, order_id: str):
    if isinstance(query, QueryOrderUseCase) and ShoppingContext.current() is not None:
        return await query.execute(order_id, buyer_id=_buyer_id())
    return await query.execute(order_id)


def _publish(broker, event_type: str, payload: dict) -> None:
    """向 broker 发布事件（broker 为空则跳过）。

    Args:
        broker: 事件总线。
        event_type: 事件类型。
        payload: 事件载荷。
    """
    if broker is not None:
        broker.publish(ShoppingContext.current_session_id(), event_type, payload)


def _jsonable(value):
    """把 Pydantic 模型 / 容器递归转换成可 JSON 序列化的结构。

    Args:
        value: 待转换的值。

    Returns:
        转换后的值。
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _to_address(data: dict) -> Address:
    """把工具入参 dict 还原成 Address 值对象。

    Args:
        data: 地址字段 dict。

    Returns:
        Address 实例。
    """
    return Address(
        recipient_name=data["recipient_name"],
        country=data["country"],
        state=data.get("state", ""),
        city=data["city"],
        address_line=data["address_line"],
        postal_code=data.get("postal_code", ""),
        phone=data.get("phone", ""),
    )


def _build_preview_payload(items: list[OrderItemArgs], shipping_address: AddressArgs) -> dict:
    """组装 order.preview 事件载荷：items + address，**不含任何单价 / 金额**。

    下单前还没有可信价格（价格由 PlaceOrderUseCase 在扣库存时从 SKU 快照），
    所以预览只回传买家已知信息；前端据此渲染「下单后由后端返回金额」占位，
    绝不自行编造价格。

    Args:
        items: 工具入参行列表。
        shipping_address: 工具入参收货地址。

    Returns:
        可 JSON 序列化的预览载荷（不含 trace_id；broker publish 时补齐）。
    """
    preview_items = [
        {
            "product_id": item.product_id,
            "sku_id": item.sku_id,
            "quantity": int(item.quantity),
        }
        for item in items
    ]
    return {
        "items": preview_items,
        "shipping_address": _jsonable(shipping_address),
    }


def _confirmation_cancelled(reason: str) -> dict:
    """构造取消 / 超时的统一返回结构。

    Args:
        reason: ``user_cancelled`` 或 ``confirmation_timeout``。

    Returns:
        ``{'cancelled': True, 'reason': reason, 'order': None}``。
    """
    return {"cancelled": True, "reason": reason, "order": None}


def _session_scope() -> str:
    """返回幂等键使用的稳定会话作用域。

    正常请求用 ``shopping_session_id``；无上下文（单测直调工具）时退回
    ``trace_id``，保证同一请求内重复调用得到同一个 key。

    Returns:
        作用域字符串。
    """
    session_id = ShoppingContext.current_session_id()
    if session_id and session_id != "anonymous":
        return session_id
    return ShoppingContext.current_trace_id() or session_id


def _idempotency_key(
    buyer_id: str, items: list[OrderItemInput], session_scope: str,
    shipping_address: dict | None = None,
) -> str:
    """生成确定性幂等键；生产 v2 绑定完整订单内容，包含全部收货地址字段。

    地址先补齐与执行相同的默认字段，再做规范 JSON 编码。旧三参调用仅保留
    兼容行为；生产调用必须传地址，避免地址变化时回放先前订单。

    Args:
        buyer_id: 买家 id。
        items: 订单行入参。
        session_scope: 会话 / trace 作用域。
        shipping_address: 完整收货地址；缺省仅兼容旧 helper 调用。

    Returns:
        sha256 前 32 位十六进制串。
    """
    if shipping_address is not None:
        payload = {
            "version": 2,
            "buyer_id": buyer_id,
            "session_scope": session_scope,
            "items": [
                {"product_id": item.product_id, "sku_id": item.sku_id, "quantity": int(item.quantity)}
                for item in items
            ],
            "shipping_address": AddressArgs.model_validate(shipping_address).model_dump(mode="json"),
        }
        canonical = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[:32]
    parts = [buyer_id]
    for item in items:
        parts.extend([item.product_id, item.sku_id, str(int(item.quantity))])
    parts.append(session_scope)
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _task_id(task_id_prefix: str, idempotency_key: str, trace_id: str) -> str:
    """生成 task 证据 id。

    Args:
        task_id_prefix: 任务 id 前缀。
        idempotency_key: 幂等键。
        trace_id: 请求级 trace id。

    Returns:
        task_id。
    """
    scope = trace_id or _session_scope()
    return f"{task_id_prefix}:{idempotency_key}:{scope}"


# ----------------------------------------------------------------------
# P9-3 幂等快路径（Redis SETNX + order_id 回填）
# ----------------------------------------------------------------------
#: 幂等键在 Redis 中的存活时间：短窗口能覆盖“用户手抖重复点击 / 并发重试”。
IDEMPOTENCY_TTL_SECONDS = 300.0

#: Redis 幂等 key 前缀（带版本号，便于后续结构升级）。
IDEMPOTENCY_KEY_PREFIX = "idem:v1"

#: 抢到 SETNX 但订单还没落库时写入的占位值。
IDEMPOTENCY_PENDING_MARKER = "__pending__"

#: 没抢到锁时，最多轮询多少次 / 每次间隔多少秒（总计约 0.5s）等并发请求回填 order_id。
IDEMPOTENCY_REPLAY_WAIT_ATTEMPTS = 10
IDEMPOTENCY_REPLAY_WAIT_INTERVAL = 0.05


def _idempotency_redis_key(idempotency_key: str) -> str:
    """幂等键 → Redis key。

    Args:
        idempotency_key: 确定性幂等键。

    Returns:
        ``idem:v1:{idempotency_key}``。
    """
    return f"{IDEMPOTENCY_KEY_PREFIX}:{idempotency_key}"


async def _acquire_idempotency_lock(redis_cache, key: str) -> bool:
    """SETNX 抢幂等锁；返回 ``True`` 表示可以继续下单。

    Redis disabled / 异常时一律按“抢到”处理（fail-safe）：
    主链路退回原有 MySQL 唯一索引路径，绝不被 Redis 阻断。

    Args:
        redis_cache: RedisCache 实例或测试替身。
        key: 幂等 Redis key。

    Returns:
        是否抢到锁。
    """
    set_if_absent = getattr(redis_cache, "set_if_absent", None)
    if not callable(set_if_absent):
        return True
    try:
        return bool(
            await set_if_absent(key, IDEMPOTENCY_PENDING_MARKER, ttl=IDEMPOTENCY_TTL_SECONDS),
        )
    except Exception:  # noqa: BLE001 - Redis 故障不阻断下单
        return True


async def _resolve_replay_order_id(
    redis_cache,
    key: str,
    *,
    attempts: int | None = None,
    interval: float | None = None,
) -> str | None:
    """没抢到锁时读取 Redis 里的 ``order_id``，必要时短暂等待并发请求回填。

    读到占位值说明另一个请求正在下单：最多等 ``attempts * interval`` 秒，
    让胜出者把 key 覆盖成真实订单号后直接回放；始终等不到则返回 ``None``，
    由调用方退回 MySQL ``find_by_idempotency_key``（唯一索引仍是最终防线）。

    Args:
        redis_cache: RedisCache 实例或测试替身。
        key: 幂等 Redis key。
        attempts: 最多轮询次数；缺省读模块常量。
        interval: 每次轮询间隔（秒）；缺省读模块常量。

    Returns:
        已有订单号；占位 / 缺失 / 超时返回 None。
    """
    get_raw = getattr(redis_cache, "get_raw", None)
    if not callable(get_raw):
        return None
    max_attempts = IDEMPOTENCY_REPLAY_WAIT_ATTEMPTS if attempts is None else attempts
    wait_interval = IDEMPOTENCY_REPLAY_WAIT_INTERVAL if interval is None else interval
    raw = await get_raw(key)
    for _ in range(max(0, max_attempts)):
        if raw is None:
            return None
        if raw != IDEMPOTENCY_PENDING_MARKER:
            return str(raw)
        await asyncio.sleep(wait_interval)
        raw = await get_raw(key)
    if raw is None or raw == IDEMPOTENCY_PENDING_MARKER:
        return None
    return str(raw)


async def _replay_snapshot(query: QueryOrderUseCase | None, order_id: str) -> dict | None:
    """按 Redis 里的 order_id 查 MySQL 回放订单快照。

    Args:
        query: 查单用例；不可用时返回 None（调用方退回幂等键路径）。
        order_id: Redis 中记录的订单号。

    Returns:
        带 ``idempotent_replay=True`` 的订单快照；查不到返回 None。
    """
    if query is None:
        return None
    try:
        snapshot = await _query_owned(query, order_id)
    except Exception:  # noqa: BLE001 - 查不到/查询失败都退回 MySQL 幂等键路径
        return None
    if not isinstance(snapshot, dict):
        return None
    return {**snapshot, "idempotent_replay": True}


async def _backfill_idempotency_key(redis_cache, key: str, order_id: str) -> bool:
    """下单成功后把 Redis 占位值覆盖成真实 ``order_id``（TTL 300s）。

    失败静默：MySQL 已是事实源，最坏只是下次多查一次库。

    Args:
        redis_cache: RedisCache 实例或测试替身。
        key: 幂等 Redis key。
        order_id: 刚创建的订单号。

    Returns:
        是否回填成功。
    """
    set_raw = getattr(redis_cache, "set_raw", None)
    if not callable(set_raw):
        return False
    try:
        return bool(await set_raw(key, order_id, ttl=IDEMPOTENCY_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        return False


async def _call_place(
    place: PlaceOrderUseCase,
    place_kwargs: dict,
    idempotency_key: str,
):
    """调用 ``place.execute``，把幂等键交给真正支持它的用例。

    P9-3 之后用例签名是
    ``execute(buyer_id, items, shipping_address, idempotency_key)``；但仍有旧调用方 /
    测试替身只实现前三参，给它们传 ``idempotency_key`` 会 ``TypeError``。

    这里不再用 ``inspect.signature`` 探测签名，改成「只对签名不兼容做一次重试」：

    1. 先带 ``idempotency_key`` 调用；生产路径的 ``PlaceOrderUseCase`` 永远命中这支，
       幂等键语义完全不变。
    2. 只有 ``TypeError`` 明确说「``idempotency_key`` 这个关键字没被接受」时才降级：
       Python 的参数绑定发生在函数体执行前，所以这次失败**没有任何副作用**，可以
       安全地去掉幂等键再调一次（旧用例不支持幂等，退回 MySQL 唯一索引路径）。
    3. 其余 ``TypeError``（用例内部抛的）原样抛出，绝不伪装成签名问题、也不重复下单。

    判据用错误措辞而不是 traceback / ``co_name``：协程的参数绑定失败发生在「调用」
    而不是「await」，栈里根本不会出现 ``execute`` 帧，按帧判定不可靠。

    Args:
        place: 下单用例（或只实现旧签名的测试替身）。
        place_kwargs: ``buyer_id`` / ``items`` / ``shipping_address`` 三个必填参数。
        idempotency_key: 确定性幂等键；仅当用例支持时才传入。

    Returns:
        ``place.execute`` 的返回值。
    """
    try:
        return await place.execute(**place_kwargs, idempotency_key=idempotency_key)
    except TypeError as err:
        # 只有「Python 明确说 idempotency_key 这个关键字没被接受」才降级重试。
        if not _unexpected_keyword(err, "idempotency_key"):
            raise
    # 旧用例不支持幂等键：去掉它再调用一次，主链路退回 MySQL 唯一索引幂等。
    return await place.execute(**place_kwargs)


def _unexpected_keyword(err: TypeError, keyword: str) -> bool:
    """判断 ``TypeError`` 是否是「调用方传了对方不接受的 keyword」。

    CPython 在参数绑定失败时抛出固定措辞：
    ``f() got an unexpected keyword argument 'kw'``（若关键字名与参数冲突则是
    ``got multiple values`` 等其它措辞）。这里只承认**点名了我们这个 keyword** 的
    「unexpected keyword argument」一类，因此用例内部抛出的 ``TypeError``（例如被调
    函数参数类型不对）永远不会被误判成签名不兼容。

    Args:
        err: 捕获到的 ``TypeError``。
        keyword: 本次调用额外传入的关键字名（这里是 ``idempotency_key``）。

    Returns:
        命中「不认识的 keyword」措辞且错误文本里出现该 keyword 时为 True。
    """
    return "unexpected keyword argument" in str(err) and keyword in str(err)


async def _record_evidence(
    task_store: TaskStateStore,
    *,
    task_id: str,
    trace_id: str,
    action: str,
    idempotency_key: str,
    order_result: dict,
    query: QueryOrderUseCase,
    order_id: str,
) -> str:
    """写 EXECUTED 证据，再用 QueryOrderUseCase 反查并升级为 VERIFIED。

    Args:
        task_store: 任务状态存储。
        task_id: 任务 id。
        trace_id: 请求级 trace id。
        action: 动作名。
        idempotency_key: 幂等键。
        order_result: create_order 得到的结果。
        query: 查单用例。
        order_id: 订单号。

    Returns:
        ``VERIFIED`` / ``EXECUTED`` / ``UNVERIFIED``。
    """
    result_json = json.dumps(_jsonable(order_result), ensure_ascii=False, sort_keys=True)
    record = TaskRecord(
        task_id=task_id,
        trace_id=trace_id,
        action=action,
        generation=1,
        status=TaskStatus.EXECUTED,
        idempotency_key=idempotency_key,
        result_json=result_json,
    )
    await task_store.put(record)

    verified_snapshot = None
    if query is not None:
        try:
            verified_snapshot = await _query_owned(query, order_id)
        except Exception:  # noqa: BLE001 - 反查失败只降级为未验证，不改变已发生的下单
            verified_snapshot = None
    if not isinstance(verified_snapshot, dict) or str(verified_snapshot.get("order_id")) != str(order_id):
        return "EXECUTED"

    verified_json = json.dumps(_jsonable(verified_snapshot), ensure_ascii=False, sort_keys=True)
    committed = await task_store.complete(
        task_id,
        record.generation,
        status=TaskStatus.VERIFIED,
        result_json=verified_json,
    )
    return "VERIFIED" if committed else "UNVERIFIED"


def build_order_tools(
    place: PlaceOrderUseCase,
    query: QueryOrderUseCase,
    cancel: CancelOrderUseCase,
    broker=None,
    task_store: TaskStateStore | None = None,
    task_id_prefix: str = "order",
    redis_cache=None,
    *,
    confirmation_manager: OrderConfirmationManager | None = None,
    settings=None,
    require_confirmation: bool = True,
) -> list[StructuredTool]:
    """构建 create_order / query_order / cancel_order 三个 LangChain 工具。

    Args:
        place: 下单用例。
        query: 查单用例。
        cancel: 取消用例。
        broker: 可选事件总线。
        task_store: 可选任务证据存储；提供时 create_order 写 EXECUTED→VERIFIED。
        task_id_prefix: 任务证据 id 前缀。
        redis_cache: 可选 RedisCache；提供时 create_order 先走 SETNX 幂等快路径。
        confirmation_manager: 确认管理器；写工具发布预览后等待真实确认并消费授权。
        settings: 可选运行时配置；用于读取 ``order_confirm_timeout_seconds``。
        require_confirmation: 默认缺少确认服务时拒绝写入。False 仅供离线业务单测，
            不会绕过已注入的管理器，也不暴露给模型；生产装配显式为 True。

    Returns:
        三个 ``StructuredTool`` 的列表。
    """
    confirm_timeout = float(
        getattr(settings, "order_confirm_timeout_seconds", DEFAULT_ORDER_CONFIRM_TIMEOUT)
        or DEFAULT_ORDER_CONFIRM_TIMEOUT,
    )

    def denied(action: str, code: str, token: str | None = None) -> dict:
        _publish(broker, "security.denied", {"action": action, "code": code})
        result = {"error_code": code, "error": "操作未获得有效确认，请重新发起并确认。", "executed": False}
        _publish(broker, "tool.result", {"tool": action, "order": result, "confirmation_token": token})
        return result

    async def approve(action: str, arguments: dict, preview: dict) -> tuple[str | None, dict | None]:
        # Model text is never approval. Only the authenticated confirmation endpoint
        # can resolve this server-owned capability, bound to exact arguments.
        if confirmation_manager is None:
            return None, denied(action, "approval_unavailable") if require_confirmation else None
        if ShoppingContext.current() is None:
            return None, denied(action, "identity_unavailable")
        binding = action_authorization(action, arguments, buyer_id=_buyer_id(),
                                       session_id=ShoppingContext.current_session_id())
        token = await confirmation_manager.create_preview({
            **preview, "buyer_id": _buyer_id(), "session_id": ShoppingContext.current_session_id(),
            "_authorization": {**binding, "expires_at": time.time() + confirm_timeout},
        })
        _publish(broker, ORDER_PREVIEW_EVENT, {"tool": action, "token": token, "preview": preview})
        try:
            outcome = await confirmation_manager.wait(token, confirm_timeout)
            if outcome != "confirmed":
                result = {**_confirmation_cancelled("user_cancelled" if outcome == "cancelled"
                                                   else "confirmation_timeout"), "executed": False}
                _publish(broker, "tool.result", {"tool": action, "order": result, "confirmation_token": token})
                return token, result
            # Recompute at the execution boundary, including current server identity.
            expected = action_authorization(action, arguments, buyer_id=_buyer_id(),
                                            session_id=ShoppingContext.current_session_id())
            if not await confirmation_manager.consume(token, expected):
                return token, denied(action, "approval_invalid", token)
            _publish(broker, "security.authorized", {"action": action, "arguments_sha256": expected["arguments_sha256"]})
            return token, None
        finally:
            await confirmation_manager.clear(token)

    async def create_order(items: list[OrderItemArgs], shipping_address: AddressArgs) -> dict:
        """创建订单（下单即 CONFIRMED），返回订单快照与验证状态。

        有 ``confirmation_manager`` 时先在 ``place.execute`` 之前发布
        ``order.preview`` 并挂起等待买家确认；取消 / 超时直接返回取消结果，
        绝不触碰 PlaceOrderUseCase。

        Args:
            items: 订单行列表。
            shipping_address: 收货地址。

        Returns:
            订单快照 dict；失败时为 ``{'error': ...}``；取消 / 超时为
            ``{'cancelled': True, 'reason': ..., 'order': None}``。
        """
        preview_payload = _build_preview_payload(items, shipping_address)
        _publish(
            broker,
            "tool.invoke",
            {"tool": "create_order", "args": preview_payload},
        )

        confirm_token, rejection = await approve("create_order", preview_payload, preview_payload)
        if rejection is not None:
            return rejection

        try:
            order_items = [
                OrderItemInput(
                    product_id=item.product_id,
                    sku_id=item.sku_id,
                    quantity=int(item.quantity),
                )
                for item in items
            ]
            buyer_id = _buyer_id()
            idempotency_key = _idempotency_key(
                buyer_id, order_items, _session_scope(), preview_payload["shipping_address"],
            )
            place_kwargs = {
                "buyer_id": buyer_id,
                "items": order_items,
                "shipping_address": _to_address(shipping_address.model_dump()),
            }

            # P9-3 幂等快路径：先抢 Redis SETNX，抢不到就尝试回放已回填的订单；
            # 抢到 / 值缺失都落到 place.execute（内部走 MySQL find_by_idempotency_key）。
            result = None
            redis_key: str | None = None
            if redis_cache is not None and idempotency_key:
                redis_key = _idempotency_redis_key(idempotency_key)
                acquired = await _acquire_idempotency_lock(redis_cache, redis_key)
                if not acquired:
                    replay_order_id = await _resolve_replay_order_id(redis_cache, redis_key)
                    if replay_order_id is not None:
                        result = await _replay_snapshot(query, replay_order_id)
            if result is None:
                result = await _call_place(place, place_kwargs, idempotency_key)
        except (ValueError, KeyError, TypeError) as err:
            _publish(broker, "tool.result", {"tool": "create_order", "error": str(err), "confirmation_token": confirm_token})
            return {"error": str(err)}

        # 成功回填 key→order_id（TTL 300s），让下一次命中直接回放。
        if (
            redis_key is not None
            and isinstance(result, dict)
            and result.get("order_id")
        ):
            await _backfill_idempotency_key(redis_cache, redis_key, str(result["order_id"]))

        # 有订单结果和证据存储时，写 EXECUTED 并反查升级为 VERIFIED。
        if task_store is not None and isinstance(result, dict) and result.get("order_id"):
            trace_id = ShoppingContext.current_trace_id()
            try:
                verification_status = await _record_evidence(
                    task_store,
                    task_id=_task_id(task_id_prefix, idempotency_key, trace_id),
                    trace_id=trace_id,
                    action="create_order",
                    idempotency_key=idempotency_key,
                    order_result=result,
                    query=query,
                    order_id=str(result["order_id"]),
                )
            except Exception:  # noqa: BLE001 - 证据落库失败不能回滚已经发生的订单
                verification_status = "UNVERIFIED"
            result = {
                **result,
                "idempotent_replay": bool(result.get("idempotent_replay", False)),
                "verification_status": verification_status,
            }

        _publish(broker, "tool.result", {"tool": "create_order", "order": result, "confirmation_token": confirm_token})
        return result

    async def query_order(order_id: str) -> dict:
        """按订单号查询订单。

        Args:
            order_id: 订单号。

        Returns:
            订单快照 dict，失败时为 ``{'error': ...}``。
        """
        _publish(
            broker,
            "tool.invoke",
            {"tool": "query_order", "args": {"order_id": order_id}},
        )
        try:
            result = await _query_owned(query, order_id)
        except (ValueError, PermissionError) as err:
            _publish(broker, "tool.result", {"tool": "query_order", "error": str(err)})
            return {"error": str(err)}
        _publish(broker, "tool.result", {"tool": "query_order", "order": result})
        return result

    async def cancel_order(order_id: str, reason: str) -> dict:
        """取消 CONFIRMED 订单并回补库存。

        Args:
            order_id: 订单号。
            reason: 取消原因。

        Returns:
            订单快照 dict，失败时为 ``{'error': ...}``。
        """
        _publish(
            broker,
            "tool.invoke",
            {
                "tool": "cancel_order",
                "args": {"order_id": order_id, "reason": reason},
            },
        )
        confirm_token = None
        try:
            arguments = {"order_id": order_id, "reason": reason}
            # A trusted read populates the preview; ownership is rechecked by the
            # transactional use case when the confirmed operation actually executes.
            if confirmation_manager is not None:
                snapshot = await _query_owned(query, order_id)
            else:
                snapshot = None
            confirm_token, rejection = await approve("cancel_order", arguments, {**arguments, "order": snapshot})
            if rejection is not None:
                return rejection
            if isinstance(cancel, CancelOrderUseCase) and ShoppingContext.current() is not None:
                result = await cancel.execute(order_id, reason, buyer_id=_buyer_id())
            else:
                result = await cancel.execute(order_id, reason)
        except (ValueError, PermissionError) as err:
            _publish(broker, "tool.result", {"tool": "cancel_order", "error": str(err), "confirmation_token": confirm_token, "order_id": order_id})
            return {"error": str(err)}
        _publish(broker, "tool.result", {"tool": "cancel_order", "order": result, "confirmation_token": confirm_token})
        return result

    return [
        StructuredTool.from_function(
            name="create_order",
            description="提出创建订单请求，服务端展示参数并等待买家确认后才执行。买家身份由系统会话上下文注入。",
            args_schema=CreateOrderArgs,
            coroutine=create_order,
        ),
        StructuredTool.from_function(
            name="query_order",
            description="按订单号查询订单详情。",
            args_schema=QueryOrderArgs,
            coroutine=query_order,
        ),
        StructuredTool.from_function(
            name="cancel_order",
            description="提出取消订单请求（仅 CONFIRMED 态），服务端等待买家确认后才取消并回补库存。",
            args_schema=CancelOrderArgs,
            coroutine=cancel_order,
        ),
    ]
