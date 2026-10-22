# -*- coding: utf-8 -*-
"""MySQL 表结构（SQLAlchemy 2.0 async）。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类。"""

    pass


class ProductRow(Base):
    """products 表：商品 JSON 载荷 + chroma_id。"""

    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(32), primary_key=True)      # SPU 主键
    payload: Mapped[dict] = mapped_column(JSON)                                # 商品聚合序列化 JSON
    chroma_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 向量库 chunk id


class OrderRow(Base):
    """orders 表：订单头与地址 / 金额快照。"""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)              # 订单主键
    buyer_id: Mapped[str] = mapped_column(String(64), index=True)                    # 买家 id
    status: Mapped[str] = mapped_column(String(16))                                  # 订单状态
    currency: Mapped[str] = mapped_column(String(8))                                 # 币种
    total_amount_minor: Mapped[int] = mapped_column(BigInteger)                      # 总金额（最小单位）
    shipping_address: Mapped[dict] = mapped_column(JSON)                             # 收货地址 JSON
    created_at: Mapped[datetime] = mapped_column(DateTime)                           # 创建时间
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 确认时间
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 取消时间
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)   # 取消原因
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # 幂等键

    __table_args__ = (
        # 数据库层唯一约束：并发重复下单的最终防线。
        Index("uq_orders_idempotency_key", "idempotency_key", unique=True),
    )


class OrderCounterRow(Base):
    """order_counters 表：订单号原子序列（P11-1）。

    一行代表一条订单号序列：``next_value`` 是“已经发出去的最大号”，取号时把它原子 +1。
    ``counter_name`` 预留多序列（例如未来按仓 / 按渠道分号段），当前固定 ``order``。
    """

    __tablename__ = "order_counters"

    counter_name: Mapped[str] = mapped_column(String(64), primary_key=True)  # 序列名：当前固定 order
    next_value: Mapped[int] = mapped_column(BigInteger, default=0)           # 已发出的最大序号
    updated_at: Mapped[datetime] = mapped_column(DateTime)                   # 最近一次取号时间


class OrderItemRow(Base):
    """order_items 表：订单行明细。"""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)  # 自增主键
    order_id: Mapped[str] = mapped_column(String(32), ForeignKey("orders.order_id"), index=True)  # 所属订单
    product_id: Mapped[str] = mapped_column(String(32))   # 商品 id
    sku_id: Mapped[str] = mapped_column(String(32))       # SKU id
    title: Mapped[str] = mapped_column(Text)              # 下单时的标题快照
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)  # 单价（最小单位）
    currency: Mapped[str] = mapped_column(String(8))      # 币种
    quantity: Mapped[int] = mapped_column(Integer)        # 数量


class BuyerPreferenceRow(Base):
    """buyer_preferences 表：买家长期偏好，buyer + kind + statement 唯一。"""

    __tablename__ = "buyer_preferences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)  # 自增主键
    buyer_id: Mapped[str] = mapped_column(String(64), index=True)  # 买家 id
    kind: Mapped[str] = mapped_column(String(16))                  # 偏好类型：like / dislike
    statement: Mapped[str] = mapped_column(String(255))            # 偏好陈述
    created_at: Mapped[str] = mapped_column(String(40))            # 创建时间（ISO 字符串）

    __table_args__ = (
        # 同一买家的同类偏好陈述去重。
        UniqueConstraint("buyer_id", "kind", "statement", name="uq_pref_buyer_kind_statement"),
    )


class UserFeedbackRow(Base):
    """user_feedback 表：每条回复的点赞 / 点踩与原因（P10-1）。"""

    __tablename__ = "user_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)  # 自增主键
    session_id: Mapped[str] = mapped_column(String(191), index=True)   # 会话 id
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 该轮请求 trace id
    buyer_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 买家 id（可空串）
    query: Mapped[str] = mapped_column(Text, default="")               # 本轮用户问题
    reply_preview: Mapped[str] = mapped_column(String(500), default="")  # 回复前 500 字预览
    score: Mapped[int] = mapped_column(Integer)                        # 1=点赞 / -1=点踩
    reason: Mapped[str] = mapped_column(String(32), default="", index=True)  # 点踩原因枚举
    created_at: Mapped[str] = mapped_column(String(40))                # 创建时间（ISO 字符串）


class SessionMetricsRow(Base):
    """session_metrics 表：每轮 ``/commerce/intents`` 的成本与用量快照（P10-5）。

    每次请求结束写一行：一轮对话用掉多少轮次 / LLM 调用 / token / 缓存命中 / 估算成本，
    供 Dashboard 做真实成本聚合与 A/B 实验对比。
    """

    __tablename__ = "session_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)  # 自增主键
    session_id: Mapped[str] = mapped_column(String(191), index=True)   # 会话 id
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 请求级 trace id
    turns: Mapped[int] = mapped_column(Integer, default=1)             # 本轮计入的轮数（通常 1）
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)         # 本轮 LLM 调用次数
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)      # 本轮 token 估算总量
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)        # 本轮语义缓存命中次数
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)   # 本轮估算成本（USD）
    created_at: Mapped[str] = mapped_column(String(40))                # 创建时间（ISO 字符串）


class AgentTaskRow(Base):
    """agent_tasks 表：任务证据 + 租约 + generation fencing。"""

    __tablename__ = "agent_tasks"

    task_id: Mapped[str] = mapped_column(String(191), primary_key=True)  # 任务主键
    trace_id: Mapped[str] = mapped_column(String(64), index=True)        # 请求级 trace id
    action: Mapped[str] = mapped_column(String(64))                      # 动作名
    generation: Mapped[int] = mapped_column(Integer)                     # fencing 代次
    status: Mapped[str] = mapped_column(String(16), index=True)          # 任务状态
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)  # 幂等键
    result_json: Mapped[str] = mapped_column(Text, default="")           # 结果 JSON
    lease_expire_at: Mapped[float] = mapped_column(Float, default=0.0)   # 租约到期时间戳
