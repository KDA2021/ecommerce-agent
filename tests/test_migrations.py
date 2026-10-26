# -*- coding: utf-8 -*-
"""P11-2 Alembic 迁移测试。

覆盖：
1. 迁移目录结构 + 初始 revision 覆盖全部业务表（纯本地，不需要数据库）；
2. 空库 ``upgrade head`` 建出全部表、版本记录为 head（需要 TEST_MYSQL_URL 且有
   CREATE DATABASE 权限，权限不足时 skip）；
3. ``downgrade base`` 回滚干净；
4. ``scripts/migrate.py`` 在空库执行成功（真实脚本进程）；
5. bootstrap_schema 兼容路径：create_all 建出来的库先 stamp 基线再 upgrade，
   不报 "table already exists"。

这些用例用**临时数据库**（ecommerce_alembic_it_<pid>）隔离，跑完即删，
绝不碰 TEST_MYSQL_URL 指向的库本身。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from app.infrastructure.persistence.db import app_table_names
from scripts.migrate import (
    MIGRATIONS_DIR,
    build_alembic_config,
    run_migrations,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"),
    reason="TEST_MYSQL_URL 未设置，跳过 Alembic 集成测试",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 初始迁移必须覆盖的全部业务表（与 app/infrastructure/persistence/tables.py 对齐）。
EXPECTED_TABLES = {
    "products",
    "orders",
    "order_items",
    "buyer_preferences",
    "user_feedback",
    "session_metrics",
    "agent_tasks",
    "order_counters",
}

TEMP_DB_NAME = f"ecommerce_alembic_it_{os.getpid()}"


# --------------------------------------------------------------------------- #
# 临时库 fixtures
# --------------------------------------------------------------------------- #
def _split_url(url: str) -> tuple[str, str, str]:
    """把 SQLAlchemy URL 拆成 ``(dialect, authority, query)``。

    例：``mysql+asyncmy://u:p@h:3307/db?charset=utf8mb4``
    -> ``("mysql+asyncmy", "u:p@h:3307", "?charset=utf8mb4")``。
    """
    assert "://" in url, f"无法解析的数据库 URL: {url}"
    dialect, rest = url.split("://", 1)
    body, _, query = rest.partition("?")
    authority = body.rsplit("/", 1)[0] if "/" in body else body
    return dialect, authority, f"?{query}" if query else ""


def _admin_url(url: str) -> str:
    """由业务库 URL 推导出连 ``mysql`` 系统库的管理 URL（同账号同主机）。"""
    dialect, host_part, query = _split_url(url)
    return f"{dialect}://{host_part}/mysql{query}"


@pytest_asyncio.fixture()
async def temp_database_url() -> str:
    """建一个空的临时库，测试结束后 drop；返回指向它的 async URL。

    需要测试账号有 ``CREATE DATABASE`` 权限（本地 compose 里的 ecommerce 账号
    已授予 ``CREATE, DROP ON *.*``）；也可用 ``TEST_MYSQL_ADMIN_URL`` 指定
    单独的管理连接串。两者都不可用时 skip，不伪装成失败。
    """
    base_url = os.environ["TEST_MYSQL_URL"]
    dialect, authority, query = _split_url(base_url)
    admin_url = os.getenv("TEST_MYSQL_ADMIN_URL") or _admin_url(base_url)
    temp_url = f"{dialect}://{authority}/{TEMP_DB_NAME}{query}"

    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {TEMP_DB_NAME}"))
            await conn.execute(
                text(
                    f"CREATE DATABASE {TEMP_DB_NAME} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                ),
            )
    except Exception as exc:  # noqa: BLE001 - 权限不足时 skip，不伪装成失败
        pytest.skip(
            f"当前 MySQL 账号无 CREATE DATABASE 权限（可设置 TEST_MYSQL_ADMIN_URL）：{exc}",
        )
    finally:
        await admin.dispose()

    yield temp_url

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {TEMP_DB_NAME}"))
    finally:
        await admin.dispose()


async def _table_names(database_url: str) -> set[str]:
    """读取目标库的表名集合。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def _alembic_version(database_url: str) -> str:
    """读取目标库的 alembic_version（不存在返回空串）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT version_num FROM alembic_version"),
            )
            return result.scalar() or ""
    finally:
        await engine.dispose()


def _run_migrate_cli(*args: str) -> subprocess.CompletedProcess:
    """用当前解释器真实执行 ``scripts/migrate.py``。

    只透传最小环境变量集 + 显式 ``--database-url``，保证结果不受调用方
    DATABASE_URL / UV_CACHE_DIR 干扰。子进程强制 UTF-8，避免中文日志在
    GBK 控制台上解码失败。
    """
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PATH": os.environ.get("PATH", ""),
    }
    for key in ("HOME", "USERPROFILE", "TEMP", "TMP", "SystemRoot"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "migrate.py"), *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


# --------------------------------------------------------------------------- #
# 1. 纯本地：迁移目录结构
# --------------------------------------------------------------------------- #
def test_initial_revision_covers_all_business_tables():
    """初始 revision 必须覆盖全部业务表，且 script_location 指向 migrations/。"""
    config = build_alembic_config()
    assert Path(config.get_main_option("script_location")).is_dir()

    versions = sorted((MIGRATIONS_DIR / "versions").glob("*.py"))
    assert versions, "migrations/versions 下没有 revision 文件"

    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("_initial_migration", versions[0])
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision is None, "第一个 revision 必须是根节点"

    source = versions[0].read_text(encoding="utf-8")
    for table in EXPECTED_TABLES:
        assert f'"{table}"' in source, f"初始迁移缺少表 {table}"
    assert EXPECTED_TABLES <= app_table_names()


# --------------------------------------------------------------------------- #
# 2/3. 空库 upgrade / downgrade
# --------------------------------------------------------------------------- #
async def test_upgrade_head_on_empty_database(temp_database_url):
    """空库执行 upgrade head：建出全部表 + 写入 head 版本。"""
    await asyncio.to_thread(run_migrations, temp_database_url, "head")

    tables = await _table_names(temp_database_url)
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables
    assert await _alembic_version(temp_database_url) == "p17_0001"

    # 再跑一次应幂等（不抛异常、版本不变）。
    await asyncio.to_thread(run_migrations, temp_database_url, "head")
    assert await _alembic_version(temp_database_url) == "p17_0001"


async def test_downgrade_to_base_drops_all_tables(temp_database_url):
    """downgrade base 后业务表全部删除，只留 alembic_version。"""
    await asyncio.to_thread(run_migrations, temp_database_url, "head")
    await asyncio.to_thread(
        run_migrations,
        temp_database_url,
        "base",
        action="downgrade",
    )

    tables = await _table_names(temp_database_url)
    assert not (EXPECTED_TABLES & tables), f"仍有残留业务表：{EXPECTED_TABLES & tables}"


# --------------------------------------------------------------------------- #
# 4. scripts/migrate.py 端到端
# --------------------------------------------------------------------------- #
async def test_migrate_script_runs_on_empty_database(temp_database_url):
    """scripts/migrate.py 以真实进程在空库执行成功。"""
    result = await asyncio.to_thread(
        _run_migrate_cli,
        "--database-url",
        temp_database_url,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert EXPECTED_TABLES <= await _table_names(temp_database_url)


# --------------------------------------------------------------------------- #
# 5. bootstrap_schema 兼容路径
# --------------------------------------------------------------------------- #
async def test_auto_stamp_compat_with_bootstrap_schema(temp_database_url):
    """bootstrap_schema 建表后，ensure_schema_at_head 走 stamp 基线且不报错。"""
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine

    engine = create_engine(temp_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    # bootstrap 路径不会写 alembic_version。
    assert "alembic_version" not in await _table_names(temp_database_url)

    result = await asyncio.to_thread(
        _run_migrate_cli,
        "--auto-stamp",
        "--database-url",
        temp_database_url,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert await _alembic_version(temp_database_url) == "p17_0001"
    assert EXPECTED_TABLES <= await _table_names(temp_database_url)
