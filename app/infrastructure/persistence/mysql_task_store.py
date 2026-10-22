# -*- coding: utf-8 -*-
"""MySQL TaskStateStore 实现。

P8-2 只依赖一张 ``agent_tasks`` 表：create_order 写 EXECUTED / VERIFIED；
P8-3 复用同一套 generation fencing 做子 Agent 租约与重试保护。
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.application.evidence.task_state import (
    TaskRecord,
    TaskStateStore,
    TaskStatus,
    _status_value,
)
from app.infrastructure.persistence.tables import AgentTaskRow


class MySqlTaskStateStore(TaskStateStore):
    """MySQL 任务证据存储。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """初始化 sessionmaker。

        Args:
            engine: 数据库引擎。
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def put(self, record: TaskRecord) -> None:
        """幂等写入任务记录；旧 generation 直接忽略。"""
        async with self._sessions() as db:
            row = await db.get(AgentTaskRow, record.task_id)
            # 禁止旧代次覆盖新代次（fencing 的第一道闸）。
            if row is not None and record.generation < row.generation:
                return
            if row is None:
                db.add(
                    AgentTaskRow(
                        task_id=record.task_id,
                        trace_id=record.trace_id,
                        action=record.action,
                        generation=record.generation,
                        status=_status_value(record.status),
                        idempotency_key=record.idempotency_key,
                        result_json=record.result_json,
                        lease_expire_at=record.lease_expire_at,
                    )
                )
            else:
                row.trace_id = record.trace_id
                row.action = record.action
                row.generation = record.generation
                row.status = _status_value(record.status)
                row.idempotency_key = record.idempotency_key
                row.result_json = record.result_json
                row.lease_expire_at = record.lease_expire_at
            try:
                await db.commit()
            except IntegrityError:
                # 并发 create_order 竞争同一 task_id 时按幂等处理。
                await db.rollback()

    async def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 task_id 查询记录。"""
        async with self._sessions() as db:
            row = await db.get(AgentTaskRow, task_id)
        return _row_to_record(row) if row is not None else None

    async def complete(
        self,
        task_id: str,
        generation: int,
        status: TaskStatus = TaskStatus.VERIFIED,
        result_json: Optional[str] = None,
    ) -> bool:
        """按 generation 提交完成状态；旧代次返回 False（fencing）。"""
        async with self._sessions() as db:
            row = await db.get(AgentTaskRow, task_id)
            # 只有当前代次能提交；更旧代次（已被接管）一律拒绝。
            if row is None or row.generation != generation:
                return False
            row.status = _status_value(status)
            if result_json is not None:
                row.result_json = result_json
            row.lease_expire_at = 0.0
            await db.commit()
        return True

    async def lease(self, task_id: str, generation: int, lease_seconds: float) -> bool:
        """续租 / generation+1 重试；不允许回退 generation。"""
        async with self._sessions() as db:
            row = await db.get(AgentTaskRow, task_id)
            # 不允许回退代次；不存在则需要先 put 登记。
            if row is None or generation < row.generation:
                return False
            if generation > row.generation:
                # 更高代次接管：重置为 PLANNED 并清空旧结果，旧代次提交随后被拒绝。
                row.generation = generation
                row.status = TaskStatus.PLANNED.value
                row.result_json = ""
            # 无论接管还是续租，都刷新租约到期时间。
            row.lease_expire_at = time.time() + lease_seconds
            await db.commit()
        return True

    async def list_by_trace(
        self,
        trace_id: str,
        status: Optional[TaskStatus] = None,
    ) -> list[TaskRecord]:
        """按 trace_id 与可选状态过滤，按 task_id 升序返回。"""
        async with self._sessions() as db:
            stmt = select(AgentTaskRow).where(AgentTaskRow.trace_id == trace_id)
            if status is not None:
                stmt = stmt.where(AgentTaskRow.status == _status_value(status))
            rows = (await db.scalars(stmt.order_by(AgentTaskRow.task_id))).all()
        return [_row_to_record(row) for row in rows]


def _row_to_record(row: AgentTaskRow) -> TaskRecord:
    """ORM 记录 → TaskRecord。

    Args:
        row: agent_tasks 记录。

    Returns:
        TaskRecord。
    """
    return TaskRecord(
        task_id=row.task_id,
        trace_id=row.trace_id,
        action=row.action,
        generation=row.generation,
        status=TaskStatus(row.status),
        idempotency_key=row.idempotency_key,
        result_json=row.result_json or "",
        lease_expire_at=float(row.lease_expire_at or 0.0),
    )
