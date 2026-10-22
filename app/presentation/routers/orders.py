# -*- coding: utf-8 -*-
"""订单路由：查询 / 取消 / 下单确认握手。

- ``GET /commerce/orders/{order_id}``：查询订单快照；
- ``POST /commerce/orders/{order_id}/cancel``：取消订单；
- ``POST /commerce/orders/confirm``：前端收到 ``order.preview`` 后确认 / 取消；
  未知 token 返回 ``status=unknown``，取消或超时不会调用 place。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.presentation.dependencies import COMMERCE_AUTH, get_container


class CancelRequest(BaseModel):
    """取消订单请求体。"""

    reason: str  # 取消原因，必填


class OrderConfirmRequest(BaseModel):
    """POST /commerce/orders/confirm 请求体（下单确认握手）。"""

    token: str                 # create_order 发布的 order.preview 里携带的 token
    action: str = "confirm"    # confirm=确认下单 / cancel=取消，不下单


def build_orders_router() -> APIRouter:
    """构造订单路由。

    Returns:
        APIRouter（查询 / 取消 / 确认）。
    """
    router = APIRouter()

    @router.get("/commerce/orders/{order_id}", dependencies=COMMERCE_AUTH)
    async def get_order(order_id: str, request: Request) -> dict:
        """查询订单。

        Args:
            order_id: 订单号。
            request: FastAPI 请求（取容器）。

        Returns:
            订单快照 dict。
        """
        container = get_container(request)
        try:
            return await container.query_order.execute(order_id)
        except ValueError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.post("/commerce/orders/{order_id}/cancel", dependencies=COMMERCE_AUTH)
    async def cancel_order(order_id: str, body: CancelRequest, request: Request) -> dict:
        """取消订单。

        Args:
            order_id: 订单号。
            body: 取消请求体。
            request: FastAPI 请求（取容器）。

        Returns:
            订单快照 dict。
        """
        container = get_container(request)
        try:
            return await container.cancel_order.execute(order_id, body.reason)
        except ValueError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    # P10-2 修复：下单确认握手。前端收到 order.preview 后点「确认下单 / 取消」，
    # 这里 resolve token 真实唤醒挂起的 create_order；取消 / 超时不会调用 place。
    @router.post("/commerce/orders/confirm", dependencies=COMMERCE_AUTH)
    async def confirm_order(body: OrderConfirmRequest, request: Request) -> dict:
        """确认 / 取消一次待下单预览。

        Args:
            body: ``{token, action}``，action 只支持 confirm / cancel。
            request: FastAPI 请求（取容器）。

        Returns:
            ``{'token', 'status', 'ok'}``；未知 token 时 status=unknown 且 ok=False。

        Raises:
            HTTPException: 确认管理器未就绪返回 503；非法 action 返回 422。
        """
        container = get_container(request)
        manager = getattr(container, "confirmation_manager", None) if container is not None else None
        if manager is None:
            raise HTTPException(status_code=503, detail="下单确认管理器未就绪")
        try:
            status = await manager.resolve(body.token, body.action)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return {"token": body.token, "status": status, "ok": status in ("confirmed", "cancelled")}

    return router
