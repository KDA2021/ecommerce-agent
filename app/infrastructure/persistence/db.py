# -*- coding: utf-8 -*-
"""数据库引擎与建表。"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.infrastructure.persistence.tables import Base


def create_engine(database_url: str) -> AsyncEngine:
    """创建 async SQLAlchemy 引擎。

    Args:
        database_url: 数据库连接串。

    Returns:
        AsyncEngine。
    """
    # pool_pre_ping：连接池借出前先探活，避免 MySQL 空闲断连导致首请求失败。
    return create_async_engine(database_url, echo=False, pool_pre_ping=True)


def app_table_names() -> set[str]:
    """返回项目 ORM 元数据里的全部业务表名。

    ``scripts/migrate.py`` 用它判断目标库是否已经由 ``bootstrap_schema``
    建过表（需要先 stamp 基线再 upgrade）。

    Returns:
        表名集合（不含 alembic_version）。
    """
    return set(Base.metadata.tables.keys())


def _orders_idempotency_migration_plan(sync_conn: Connection) -> tuple[bool, bool]:
    """检查已有 orders 表是否缺 P8-2 幂等列 / 唯一索引。

    Args:
        sync_conn: SQLAlchemy 同步连接（由 run_sync 提供）。

    Returns:
        ``(需要加列, 需要加唯一索引)``。
    """
    inspector = inspect(sync_conn)
    # 全新建库时 create_all 会直接带全列，无需迁移。
    if "orders" not in inspector.get_table_names():
        return False, False
    columns = {column["name"] for column in inspector.get_columns("orders")}
    # 旧表缺幂等列：需要同时加列与加唯一索引。
    if "idempotency_key" not in columns:
        return True, True

    unique_indexes = {
        tuple(index.get("column_names") or [])
        for index in inspector.get_indexes("orders")
        if index.get("unique")
    }
    unique_constraints = {
        tuple(constraint.get("column_names") or [])
        for constraint in inspector.get_unique_constraints("orders")
    }
    key_is_unique = ("idempotency_key",) in unique_indexes or ("idempotency_key",) in unique_constraints
    return False, not key_is_unique


async def bootstrap_schema(engine: AsyncEngine) -> None:
    """按 ORM 元数据建表，并对已有 orders 表做 P8-2 幂等列迁移（幂等）。

    Args:
        engine: 数据库引擎。
    """
    async with engine.begin() as conn:
        # create_all 只补缺失的表；旧表结构升级靠下面的显式迁移。
        await conn.run_sync(Base.metadata.create_all)
        add_column, add_unique_index = await conn.run_sync(_orders_idempotency_migration_plan)
        if add_column:
            await conn.execute(
                text("ALTER TABLE orders ADD COLUMN idempotency_key VARCHAR(128) NULL"),
            )
        if add_unique_index:
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_orders_idempotency_key "
                    "ON orders (idempotency_key)"
                ),
            )
