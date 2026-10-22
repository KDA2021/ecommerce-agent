# -*- coding: utf-8 -*-
"""MySQL 实现 PreferenceStore 端口。"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.domain.buyer.preference import BuyerPreference, PreferenceStore
from app.infrastructure.persistence.tables import BuyerPreferenceRow


class MySqlPreferenceStore(PreferenceStore):
    """MySQL 偏好存储实现（buyer_preferences 表）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """初始化 sessionmaker。

        Args:
            engine: 数据库引擎。
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def append(self, preference: BuyerPreference) -> None:
        """插入偏好；唯一约束冲突视为幂等去重。

        Args:
            preference: 买家偏好。
        """
        async with self._sessions() as db:
            db.add(BuyerPreferenceRow(
                buyer_id=preference.buyer_id,
                kind=preference.kind,
                statement=preference.statement,
                created_at=preference.created_at,
            ))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()

    async def list_by_buyer(self, buyer_id: str) -> list[BuyerPreference]:
        """按自增 id 升序列出该买家的偏好。

        Args:
            buyer_id: 买家 id。

        Returns:
            偏好列表。
        """
        async with self._sessions() as db:
            rows = (await db.scalars(
                select(BuyerPreferenceRow)
                .where(BuyerPreferenceRow.buyer_id == buyer_id)
                .order_by(BuyerPreferenceRow.id),
            )).all()
        return [
            BuyerPreference(buyer_id=r.buyer_id, kind=r.kind, statement=r.statement, created_at=r.created_at)
            for r in rows
        ]

    async def delete(self, buyer_id: str, statement: str) -> bool:
        """按 buyer + statement 精确删除。

        Args:
            buyer_id: 买家 id。
            statement: 偏好原文。

        Returns:
            是否删除了记录。
        """
        async with self._sessions() as db:
            result = await db.execute(
                delete(BuyerPreferenceRow).where(
                    BuyerPreferenceRow.buyer_id == buyer_id,
                    BuyerPreferenceRow.statement == statement,
                ),
            )
            await db.commit()
        return bool(result.rowcount)
