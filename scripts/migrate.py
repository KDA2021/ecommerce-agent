# -*- coding: utf-8 -*-
"""运行 Alembic 迁移：项目封装入口（P11-2）。

用法::

    uv run python scripts/migrate.py                      # 默认 upgrade head
    uv run python scripts/migrate.py --revision p11_0001  # 升/降到指定版本
    uv run python scripts/migrate.py --stamp head         # 只打版本号，不动结构
    uv run python scripts/migrate.py --sql                # 只打印 SQL（offline）
    uv run python scripts/migrate.py -x url=...           # 临时指向别的库

为什么需要它：
- 统一入口，避免每次手写一长串 ``alembic -c ... upgrade head``；
- 目标库默认取 ``DATABASE_URL``（与运行时同一个 Settings 来源）；
- 兼容 ``bootstrap_schema`` 快速路径：库是开发/测试用 create_all 建出来的
  （没有 ``alembic_version``）时，自动 ``stamp head`` 记录基线，再升级；
- 既可直接执行，也可被测试 ``import`` 调用（``run_migrations`` / ``main``）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

# 直接执行时保证能 import app.*（脚本在 scripts/ 下，项目根是上一级）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.infrastructure.persistence.db import app_table_names  # noqa: E402
from app.infrastructure.settings import load_settings  # noqa: E402

#: 迁移脚本所在目录（含 env.py / versions/）。
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def build_alembic_config(database_url: Optional[str] = None) -> Config:
    """构造指向本项目 ``migrations/`` 的 Alembic Config。

    Args:
        database_url: 目标库连接串；为空时由 ``migrations/env.py`` 从
            ``DATABASE_URL``（Settings）解析。

    Returns:
        配置好的 ``alembic.config.Config``。
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # ``%(here)s`` 依赖 ini 路径，显式再设一次 script_location，避免调用方 cwd 不同。
    config.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    if database_url:
        # 与 env.py 的 -x url=... 同一条优先级最高的路径，无需改环境变量。
        config.cmd_opts = argparse.Namespace(x=[f"url={database_url}"])
    return config


async def database_has_app_tables(database_url: str) -> bool:
    """判断目标库是否已有业务表（由 ``bootstrap_schema`` 等建的）。

    Args:
        database_url: 目标库连接串。

    Returns:
        存在任意业务表返回 True（含 alembic_version 不算）。
    """
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    finally:
        await engine.dispose()
    return "alembic_version" not in tables and bool(set(tables) & app_table_names())


def run_migrations(
    database_url: Optional[str] = None,
    revision: str = "head",
    *,
    action: str = "upgrade",
    sql: bool = False,
) -> None:
    """执行一次 Alembic 操作。

    Args:
        database_url: 目标库连接串；为空时用 ``DATABASE_URL``。
        revision: 目标版本（``head`` / ``base`` / 具体 revision id）。
        action: ``upgrade`` | ``downgrade`` | ``stamp``。
        sql: 仅 upgrade/downgrade 有效，为 True 时走 offline（只打印 SQL）。
    """
    config = build_alembic_config(database_url)
    if action == "upgrade":
        command.upgrade(config, revision, sql=sql)
    elif action == "downgrade":
        command.downgrade(config, revision, sql=sql)
    elif action == "stamp":
        command.stamp(config, revision)
    else:  # pragma: no cover - argparse 已限定取值
        raise ValueError(f"unknown action: {action}")


def ensure_schema_at_head(database_url: Optional[str] = None) -> str:
    """把库升级到 ``head``，并兼容 ``bootstrap_schema`` 建出来的库。

    分两种情况：
    1. **空库 / 已有 alembic_version**：直接 ``upgrade head``。
    2. **有业务表但没有 alembic_version**（开发/测试走 ``bootstrap_schema``）：
       先标记 ``p11_0001`` 初始基线，再依次运行后续增量迁移。
       否则初始迁移的 ``CREATE TABLE`` 会因为表已存在而失败。

    Args:
        database_url: 目标库连接串；为空时用 ``DATABASE_URL``。

    Returns:
        实际使用的连接串（供日志/测试断言）。
    """
    url = database_url or load_settings().database_url
    if asyncio.run(database_has_app_tables(url)):
        # stamp 初始基线只写版本号，后续增量迁移仍会检查并补齐缺失结构。
        print(
            "[migrate] 检测到已有业务表但没有 Alembic 版本记录："
            "先 stamp p11_0001 记录初始基线，再运行后续迁移。",
            file=sys.stderr,
        )
        run_migrations(url, "p11_0001", action="stamp")
    run_migrations(url, "head", action="upgrade")
    return url


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python scripts/migrate.py",
        description="运行 Alembic 迁移（默认 upgrade head）。",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="目标库连接串；默认取 DATABASE_URL 环境变量 / .env。",
    )
    parser.add_argument(
        "--revision",
        default="head",
        help="目标版本，默认 head（可写 base 或具体 revision id）。",
    )
    parser.add_argument(
        "--action",
        choices=("upgrade", "downgrade", "stamp"),
        default="upgrade",
        help="要执行的操作，默认 upgrade。",
    )
    parser.add_argument(
        "--sql",
        action="store_true",
        help="offline 模式：只把 SQL 打印到 stdout，不连库。",
    )
    parser.add_argument(
        "--auto-stamp",
        action="store_true",
        help="兼容 bootstrap_schema 建出来的库：先 stamp head 再 upgrade。",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """脚本入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码：0 成功，1 失败（异常已打印）。
    """
    args = build_parser().parse_args(argv)
    url = args.database_url or os.getenv("DATABASE_URL") or load_settings().database_url
    try:
        if args.sql:
            run_migrations(url, args.revision, action=args.action, sql=True)
        elif args.action == "upgrade" and args.auto_stamp:
            # 显式开启兼容路径：库可能来自 bootstrap_schema（无 alembic_version）。
            ensure_schema_at_head(url)
        else:
            run_migrations(url, args.revision, action=args.action)
    except Exception as exc:  # noqa: BLE001 - CLI 需要打印可读错误并返回非零
        print(f"[migrate] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[migrate] {args.action} {args.revision} 完成：{_mask_url(url)}")
    return 0


def _mask_url(url: str) -> str:
    """日志用的脱敏连接串（只保留驱动 + 库名）。"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _, _, tail = rest.rpartition("@")
    return f"{scheme}://***@{tail}"


if __name__ == "__main__":
    raise SystemExit(main())
