# -*- coding: utf-8 -*-
"""MySQL 实现 MetricsStore 端口（session_metrics 表）。

每轮 ``/commerce/intents`` 结束写一行；Dashboard 通过 ``aggregate()`` 读取真实
成本 / 用量指标。聚合口径见 ``app.application.metrics.session_metrics``。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.application.metrics.session_metrics import MetricsStore, SessionMetrics
from app.infrastructure.persistence.tables import SessionMetricsRow


class MySqlMetricsStore(MetricsStore):
    """session_metrics 的 MySQL 存储实现。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """初始化 sessionmaker。

        Args:
            engine: 数据库引擎。
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, metrics: SessionMetrics) -> int:
        """插入一行会话指标并返回自增 id。

        Args:
            metrics: 指标值对象。

        Returns:
            新记录 id。
        """
        async with self._sessions() as db:
            row = SessionMetricsRow(
                session_id=metrics.session_id,
                trace_id=metrics.trace_id,
                turns=metrics.turns,
                llm_calls=metrics.llm_calls,
                total_tokens=metrics.total_tokens,
                cache_hits=metrics.cache_hits,
                cost_estimate=metrics.cost_estimate,
                created_at=metrics.created_at,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return int(row.id)

    async def list_by_session(self, session_id: str, limit: int = 100) -> list[SessionMetrics]:
        """按会话 id 查询指标（自增 id 正序）。

        Args:
            session_id: 会话 id。
            limit: 最多返回条数。

        Returns:
            指标列表。
        """
        async with self._sessions() as db:
            rows = (await db.scalars(
                select(SessionMetricsRow)
                .where(SessionMetricsRow.session_id == session_id)
                .order_by(SessionMetricsRow.id)
                .limit(max(int(limit), 1)),
            )).all()
        return [_row_to_metrics(row) for row in rows]

    async def list_all(self, limit: int | None = None) -> list[SessionMetrics]:
        """查询全部指标（自增 id 正序）。

        Args:
            limit: 可选条数上限。

        Returns:
            指标列表。
        """
        stmt = select(SessionMetricsRow).order_by(SessionMetricsRow.id)
        if limit is not None:
            stmt = stmt.limit(max(int(limit), 1))
        async with self._sessions() as db:
            rows = (await db.scalars(stmt)).all()
        return [_row_to_metrics(row) for row in rows]

    async def delete(self, metrics_id: int) -> bool:
        """按 id 删除一行指标（测试 / 运维清理用）。

        Args:
            metrics_id: 指标 id。

        Returns:
            是否命中删除。
        """
        async with self._sessions() as db:
            row = await db.get(SessionMetricsRow, metrics_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
        return True


def _row_to_metrics(row: SessionMetricsRow) -> SessionMetrics:
    """ORM 记录 -> SessionMetrics 值对象。

    Args:
        row: session_metrics 记录。

    Returns:
        SessionMetrics。
    """
    return SessionMetrics(
        session_id=row.session_id,
        trace_id=row.trace_id or "",
        turns=int(row.turns or 0),
        llm_calls=int(row.llm_calls or 0),
        total_tokens=int(row.total_tokens or 0),
        cache_hits=int(row.cache_hits or 0),
        cost_estimate=float(row.cost_estimate or 0.0),
        created_at=row.created_at or "",
        id=int(row.id),
    )
