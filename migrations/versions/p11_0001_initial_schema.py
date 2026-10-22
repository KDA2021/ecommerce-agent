# -*- coding: utf-8 -*-
"""initial schema

P11-2 初始迁移：把当前 ORM 元数据里的全部业务表建出来。

覆盖表（与 app/infrastructure/persistence/tables.py 一一对应）：
- products            商品 JSON 载荷 + chroma_id
- orders              订单头（含 P8-2 幂等键唯一索引 / P10-2 取消字段）
- order_items         订单行明细（FK -> orders.order_id）
- buyer_preferences   买家长期偏好（buyer_id + kind + statement 唯一）
- user_feedback       每条回复的点赞/点踩（P10-1）
- session_metrics     每轮成本与用量快照（P10-5）
- agent_tasks         任务证据 + 租约 + generation fencing（P8-3）
- order_counters      订单号原子序列（P11-1）

Revision ID: p11_0001
Revises:
Create Date: 2026-10-11 10:34:11.480462

说明：本文件由 ``alembic revision --autogenerate`` 生成后人工整理，
**不要**重新 autogenerate 覆盖它；后续结构变更新增 revision 即可。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p11_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建立全部业务表（空库初始化）。"""
    op.create_table(
        "agent_tasks",
        sa.Column("task_id", sa.String(length=191), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("lease_expire_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        op.f("ix_agent_tasks_idempotency_key"),
        "agent_tasks",
        ["idempotency_key"],
        unique=False,
    )
    op.create_index(op.f("ix_agent_tasks_status"), "agent_tasks", ["status"], unique=False)
    op.create_index(op.f("ix_agent_tasks_trace_id"), "agent_tasks", ["trace_id"], unique=False)

    op.create_table(
        "buyer_preferences",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("buyer_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("statement", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "buyer_id",
            "kind",
            "statement",
            name="uq_pref_buyer_kind_statement",
        ),
    )
    op.create_index(
        op.f("ix_buyer_preferences_buyer_id"),
        "buyer_preferences",
        ["buyer_id"],
        unique=False,
    )

    op.create_table(
        "order_counters",
        sa.Column("counter_name", sa.String(length=64), nullable=False),
        sa.Column("next_value", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("counter_name"),
    )

    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("buyer_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("total_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("shipping_address", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("order_id"),
    )
    op.create_index(op.f("ix_orders_buyer_id"), "orders", ["buyer_id"], unique=False)
    # P8-2：并发重复下单的最终防线，数据库层唯一约束。
    op.create_index("uq_orders_idempotency_key", "orders", ["idempotency_key"], unique=True)

    op.create_table(
        "products",
        sa.Column("product_id", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("chroma_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("product_id"),
    )

    op.create_table(
        "session_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=191), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_hits", sa.Integer(), nullable=False),
        sa.Column("cost_estimate", sa.Float(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_session_metrics_session_id"),
        "session_metrics",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_metrics_trace_id"),
        "session_metrics",
        ["trace_id"],
        unique=False,
    )

    op.create_table(
        "user_feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=191), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_id", sa.String(length=64), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("reply_preview", sa.String(length=500), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_feedback_buyer_id"),
        "user_feedback",
        ["buyer_id"],
        unique=False,
    )
    op.create_index(op.f("ix_user_feedback_reason"), "user_feedback", ["reason"], unique=False)
    op.create_index(
        op.f("ix_user_feedback_session_id"),
        "user_feedback",
        ["session_id"],
        unique=False,
    )
    op.create_index(op.f("ix_user_feedback_trace_id"), "user_feedback", ["trace_id"], unique=False)

    # order_items 最后建：外键依赖 orders.order_id 已存在。
    # FK 不显式命名，交给 MySQL 自动生成（ORM 侧同样是匿名 FK，两边保持一致）。
    op.create_table(
        "order_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.String(length=32), nullable=False),
        sa.Column("sku_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)


def downgrade() -> None:
    """按依赖倒序删除全部业务表。

    注意：``order_items`` 只 drop_table、不单独 drop_index —— MySQL 不允许删除
    仍被外键依赖的索引；drop_table 会连带清掉该表的 FK 与索引，最稳妥。
    """
    op.drop_table("order_items")

    op.drop_index(op.f("ix_user_feedback_trace_id"), table_name="user_feedback")
    op.drop_index(op.f("ix_user_feedback_session_id"), table_name="user_feedback")
    op.drop_index(op.f("ix_user_feedback_reason"), table_name="user_feedback")
    op.drop_index(op.f("ix_user_feedback_buyer_id"), table_name="user_feedback")
    op.drop_table("user_feedback")

    op.drop_index(op.f("ix_session_metrics_trace_id"), table_name="session_metrics")
    op.drop_index(op.f("ix_session_metrics_session_id"), table_name="session_metrics")
    op.drop_table("session_metrics")

    op.drop_table("products")

    op.drop_index("uq_orders_idempotency_key", table_name="orders")
    op.drop_index(op.f("ix_orders_buyer_id"), table_name="orders")
    op.drop_table("orders")

    op.drop_table("order_counters")

    op.drop_index(op.f("ix_buyer_preferences_buyer_id"), table_name="buyer_preferences")
    op.drop_table("buyer_preferences")

    op.drop_index(op.f("ix_agent_tasks_trace_id"), table_name="agent_tasks")
    op.drop_index(op.f("ix_agent_tasks_status"), table_name="agent_tasks")
    op.drop_index(op.f("ix_agent_tasks_idempotency_key"), table_name="agent_tasks")
    op.drop_table("agent_tasks")
