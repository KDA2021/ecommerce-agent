# -*- coding: utf-8 -*-
"""MySQL 实现 FeedbackStore 端口（user_feedback 表）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.domain.feedback.feedback import FeedbackStore, UserFeedback
from app.infrastructure.persistence.tables import UserFeedbackRow


class MySqlFeedbackStore(FeedbackStore):
    """MySQL 反馈存储实现。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """初始化 sessionmaker。

        Args:
            engine: 数据库引擎。
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, feedback: UserFeedback) -> int:
        """插入一条反馈并返回自增 id。

        Args:
            feedback: 反馈值对象。

        Returns:
            新记录 id。
        """
        async with self._sessions() as db:
            row = UserFeedbackRow(
                session_id=feedback.session_id,
                trace_id=feedback.trace_id,
                buyer_id=feedback.buyer_id,
                query=feedback.query,
                reply_preview=feedback.reply_preview,
                score=feedback.score,
                reason=feedback.reason,
                created_at=feedback.created_at,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return int(row.id)

    async def list_by_session(self, session_id: str, limit: int = 100) -> list[UserFeedback]:
        """按会话 id 查询反馈（自增 id 正序）。

        Args:
            session_id: 会话 id。
            limit: 最多返回条数。

        Returns:
            反馈列表。
        """
        async with self._sessions() as db:
            rows = (await db.scalars(
                select(UserFeedbackRow)
                .where(UserFeedbackRow.session_id == session_id)
                .order_by(UserFeedbackRow.id)
                .limit(max(int(limit), 1)),
            )).all()
        return [_row_to_feedback(row) for row in rows]

    async def list_all(self, limit: int | None = None) -> list[UserFeedback]:
        """查询全部反馈，供 Dashboard 聚合。

        Args:
            limit: 可选条数上限。

        Returns:
            反馈列表（自增 id 正序）。
        """
        stmt = select(UserFeedbackRow).order_by(UserFeedbackRow.id)
        if limit is not None:
            stmt = stmt.limit(max(int(limit), 1))
        async with self._sessions() as db:
            rows = (await db.scalars(stmt)).all()
        return [_row_to_feedback(row) for row in rows]

    async def delete(self, feedback_id: int) -> bool:
        """按 id 删除反馈（测试 / 运维清理用）。

        Args:
            feedback_id: 反馈 id。

        Returns:
            是否命中删除。
        """
        async with self._sessions() as db:
            row = await db.get(UserFeedbackRow, feedback_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
        return True


def _row_to_feedback(row: UserFeedbackRow) -> UserFeedback:
    """ORM 记录 -> UserFeedback 值对象。

    Args:
        row: user_feedback 记录。

    Returns:
        UserFeedback。
    """
    return UserFeedback(
        session_id=row.session_id,
        score=int(row.score),
        query=row.query or "",
        trace_id=row.trace_id or "",
        reply_preview=row.reply_preview or "",
        reason=row.reason or "",
        buyer_id=row.buyer_id or "",
        created_at=row.created_at or "",
        id=int(row.id),
    )
