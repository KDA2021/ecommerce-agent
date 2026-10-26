# -*- coding: utf-8 -*-
"""MySQL 实现 PreferenceStore 端口。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import LargeBinary, cast, delete, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.domain.buyer.preference import BuyerPreference, PreferenceStore
from app.domain.buyer.preference_facts import safe_preference
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
        safe = safe_preference(preference)
        if (
            safe is None or not preference.attribute or preference.value is None
            or not preference.buyer_id or len(preference.buyer_id) > 64
            or not preference.source_session_id or len(preference.source_session_id) > 191
        ):
            raise ValueError("只能保存带会话来源的结构化商品偏好")
        now = datetime.now(timezone.utc).isoformat()
        values = dict(
            buyer_id=safe.buyer_id, kind=safe.kind, statement=safe.statement,
            created_at=now, attribute=safe.attribute, value=safe.value,
            currency=safe.currency, source_session_id=safe.source_session_id,
            updated_at=now,
        )
        statement = insert(BuyerPreferenceRow).values(**values)
        # Retain the first creation time while refreshing server provenance on
        # an explicit repeat. The existing buyer/kind/canonical-statement unique
        # key serializes concurrent duplicate writes.
        statement = statement.on_duplicate_key_update(**{
            name: values[name] for name in
            ("attribute", "value", "currency", "source_session_id", "updated_at")
        })
        async with self._sessions() as db:
            await db.execute(statement)
            await db.commit()

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
                .where(cast(BuyerPreferenceRow.buyer_id, LargeBinary) == buyer_id.encode("utf-8"))
                .order_by(BuyerPreferenceRow.id),
            )).all()
        return [
            BuyerPreference(
                buyer_id=r.buyer_id, kind=r.kind, statement=r.statement, created_at=r.created_at,
                attribute=r.attribute, value=r.value, currency=r.currency,
                source_session_id=r.source_session_id, updated_at=r.updated_at,
            )
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
                    cast(BuyerPreferenceRow.buyer_id, LargeBinary) == buyer_id.encode("utf-8"),
                    cast(BuyerPreferenceRow.statement, LargeBinary) == statement.encode("utf-8"),
                ),
            )
            await db.commit()
        return bool(result.rowcount)
