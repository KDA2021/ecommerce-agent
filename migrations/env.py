# -*- coding: utf-8 -*-
"""Alembic 运行环境：async 引擎 + 项目 ORM metadata + DATABASE_URL。

设计要点：
- **单一事实源**：metadata 取自 ``app.infrastructure.persistence.tables.Base.metadata``，
  连接串取自 ``app.infrastructure.settings``（环境变量 / .env）；本文件不维护第二份表结构或 URL。
- **async 优先**：默认走 ``async_engine_from_config`` + ``connection.run_sync``，与运行时
  asyncmy 驱动一致；传入同步 URL（例如 sqlite://）时自动退回同步引擎，方便轻量验证。
- **offline 可用**：``alembic upgrade head --sql`` 只需要 URL 字符串，不需要 DBAPI。
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Optional

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.infrastructure.persistence.tables import Base
from app.infrastructure.settings import load_settings

# --------------------------------------------------------------------------- #
# Alembic Config 与 metadata
# --------------------------------------------------------------------------- #
config = context.config

# 只在通过 alembic CLI 运行时初始化日志；被其它脚本 import 时不重复配置。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的目标：项目全部 ORM 表（products/orders/order_items/buyer_preferences/
# user_feedback/session_metrics/agent_tasks/order_counters）。
target_metadata = Base.metadata

#: async 驱动 -> 对应方言的「同步等价 URL」。offline 模式（--sql）只会把这个 URL
#: 交给方言编译器，因此必须换成有同步 DBAPI 的前缀。
_SYNC_DRIVER_EQUIVALENTS = {
    "mysql+asyncmy": "mysql",
    "mysql+aiomysql": "mysql+pymysql",
    "postgresql+asyncpg": "postgresql",
    "sqlite+aiosqlite": "sqlite",
}


def _x_argument(name: str) -> Optional[str]:
    """安全读取 ``alembic -x name=value`` 参数（环境外调用时不存在）。"""
    try:
        return context.get_x_argument(as_dictionary=True).get(name)
    except Exception:  # noqa: BLE001 - 非 alembic 运行环境没有该 API
        return None


def resolve_database_url() -> str:
    """解析迁移目标库的连接串。

    优先级：``alembic -x url=...`` > ``config.get_main_option("sqlalchemy.url")``
    > ``app.infrastructure.settings.load_settings().database_url``（环境变量 / .env）。

    Returns:
        数据库连接串。
    """
    url = _x_argument("url") or config.get_main_option("sqlalchemy.url") or ""
    if not url.strip() or url == "driver://user:pass@localhost/dbname":
        url = load_settings().database_url
    return url.strip()


def to_offline_url(url: str) -> str:
    """把 async URL 转成同步等价 URL，供 offline（--sql）模式使用。"""
    for async_driver, sync_driver in _SYNC_DRIVER_EQUIVALENTS.items():
        if url.startswith(f"{async_driver}://"):
            return f"{sync_driver}://{url[len(async_driver) + 3:]}"
    return url


def is_async_url(url: str) -> bool:
    """判断 URL 是否使用 async 驱动。"""
    dialect = url.split("://", 1)[0]
    return dialect in _SYNC_DRIVER_EQUIVALENTS or "+async" in dialect


def _inject_url(url: str) -> None:
    """把 URL 注入 Alembic Config。

    ``config.set_main_option`` 走 ConfigParser，值里的 ``%`` 会被当成插值语法；
    先替换成 ``%%`` 是 Alembic 官方推荐的写法（连接串里的密码可能含 %）。
    """
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


# --------------------------------------------------------------------------- #
# 迁移执行
# --------------------------------------------------------------------------- #
def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（``alembic upgrade head --sql``）。"""
    context.configure(
        url=to_offline_url(resolve_database_url()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步连接的上下文中执行迁移（由 ``run_sync`` 调用）。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations(url: str) -> None:
    """用 async 引擎执行迁移。

    Args:
        url: async 数据库 URL。
    """
    _inject_url(url)
    # 迁移是短命的批处理任务：NullPool 避免连接被池化后残留。
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移；async URL 走 async 引擎，同步 URL 走同步引擎。"""
    url = resolve_database_url()
    if is_async_url(url):
        asyncio.run(run_async_migrations(url))
        return

    _inject_url(url)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
