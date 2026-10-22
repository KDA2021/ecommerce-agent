# -*- coding: utf-8 -*-
"""任务状态值对象与 TaskStateStore 端口。

P8-2 用证据链回答“订单到底有没有真正落库”：create_order 成功后先写
``EXECUTED``，再用仓储反查订单存在后升级为 ``VERIFIED``。服务层只信任
当前 trace 下的 ``VERIFIED`` 记录，避免模型把工具调用文本当成完成事实。

generation fencing 由 ``put/lease/complete`` 共同维护：
- ``lease`` 可以把同一个 task 的 generation 推高（重试 +1）；
- ``complete`` 只接受当前 generation，旧代次提交返回 False；
- ``put`` 不允许写回比库里更旧的 generation。
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    """任务状态：PLANNED / EXECUTED / VERIFIED / FAILED / UNKNOWN。"""

    PLANNED = "PLANNED"    # 已登记、等待执行
    EXECUTED = "EXECUTED"  # 已执行、尚未反查验证
    VERIFIED = "VERIFIED"  # 已用外部存储反查验证
    FAILED = "FAILED"      # 明确失败
    UNKNOWN = "UNKNOWN"    # 超时等结果未知（不重试）


def _status_value(status: TaskStatus | str) -> str:
    """把 TaskStatus / 字符串统一成数据库字符串值。

    Args:
        status: 任务状态。

    Returns:
        状态字符串。
    """
    return status.value if isinstance(status, TaskStatus) else str(status)


def _coerce_status(status: TaskStatus | str) -> TaskStatus:
    """把 TaskStatus / 字符串统一成枚举。

    Args:
        status: 任务状态。

    Returns:
        TaskStatus。
    """
    return status if isinstance(status, TaskStatus) else TaskStatus(status)


@dataclass
class TaskRecord:
    """一条任务证据记录。"""

    task_id: str                    # 任务唯一 id（子任务 / 订单证据）
    trace_id: str                   # 请求级 trace id，完成门控按它聚合
    action: str                     # 动作名，如 create_order
    generation: int                 # 代次：重试 +1，用于 fencing
    status: TaskStatus              # 当前任务状态
    idempotency_key: str            # 幂等键，关联订单去重
    result_json: str = ""           # 落库结果快照 JSON（VERIFIED 后含订单号）
    lease_expire_at: float = 0.0    # 租约到期时间戳；0 表示无租约

    def verification_order_id(self) -> Optional[str]:
        """从 ``result_json`` 解析被验证的订单号。

        Returns:
            订单号；没有订单字段或 JSON 非法时返回 None。
        """
        try:
            payload = json.loads(self.result_json or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        order_id = payload.get("order_id")
        if not order_id:
            order = payload.get("order")
            if isinstance(order, dict):
                order_id = order.get("order_id")
        return str(order_id) if order_id else None


class TaskStateStore(ABC):
    """TaskStateStore 端口：任务证据持久化 + 租约 + generation fencing。"""

    @abstractmethod
    async def put(self, record: TaskRecord) -> None:
        """幂等写入任务记录；不允许用旧 generation 覆盖新记录。

        Args:
            record: 任务记录。
        """
        ...

    @abstractmethod
    async def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 task_id 查询记录。

        Args:
            task_id: 任务 id。

        Returns:
            TaskRecord；未命中返回 None。
        """
        ...

    @abstractmethod
    async def complete(
        self,
        task_id: str,
        generation: int,
        status: TaskStatus = TaskStatus.VERIFIED,
        result_json: Optional[str] = None,
    ) -> bool:
        """按 generation 提交完成状态；旧代次返回 False（fencing）。

        Args:
            task_id: 任务 id。
            generation: 调用方持有的代次。
            status: 完成状态，默认 VERIFIED。
            result_json: 可选结果 JSON；为 None 时保留旧值。

        Returns:
            是否提交成功。
        """
        ...

    @abstractmethod
    async def lease(self, task_id: str, generation: int, lease_seconds: float) -> bool:
        """续租 / 提升 generation。

        Args:
            task_id: 任务 id。
            generation: 本次调用想持有的代次；大于当前值表示重试 +1。
            lease_seconds: 租约时长（秒）。

        Returns:
            是否获得租约。
        """
        ...

    @abstractmethod
    async def list_by_trace(
        self,
        trace_id: str,
        status: Optional[TaskStatus] = None,
    ) -> list[TaskRecord]:
        """按 trace_id 列出任务记录，可选按状态过滤。

        Args:
            trace_id: 请求级 trace id。
            status: 可选任务状态。

        Returns:
            TaskRecord 列表（task_id 升序）。
        """
        ...

    async def list_verified_order_ids(self, trace_id: str) -> list[str]:
        """返回该 trace 下所有 VERIFIED 记录里的订单号。

        Args:
            trace_id: 请求级 trace id。

        Returns:
            去重后的订单号列表。
        """
        records = await self.list_by_trace(trace_id, TaskStatus.VERIFIED)
        order_ids: list[str] = []
        for record in records:
            order_id = record.verification_order_id()
            if order_id and order_id not in order_ids:
                order_ids.append(order_id)
        return order_ids

    async def verified_order_ids(self, trace_id: str) -> list[str]:
        """``list_verified_order_ids`` 的语义别名，便于服务层直接读取。

        Args:
            trace_id: 请求级 trace id。

        Returns:
            去重后的订单号列表。
        """
        return await self.list_verified_order_ids(trace_id)


class InMemoryTaskStateStore(TaskStateStore):
    """内存 TaskStateStore：无数据库测试与本地演示使用。"""

    def __init__(self) -> None:
        """初始化 task_id → TaskRecord 字典。"""
        self._records: dict[str, TaskRecord] = {}

    async def put(self, record: TaskRecord) -> None:
        """幂等写入；旧 generation 直接忽略。"""
        record = replace(record, status=_coerce_status(record.status))
        existing = self._records.get(record.task_id)
        if existing is not None and record.generation < existing.generation:
            return
        self._records[record.task_id] = replace(record)

    async def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 task_id 查询并返回副本。"""
        record = self._records.get(task_id)
        return replace(record) if record is not None else None

    async def complete(
        self,
        task_id: str,
        generation: int,
        status: TaskStatus = TaskStatus.VERIFIED,
        result_json: Optional[str] = None,
    ) -> bool:
        """按 generation 提交完成状态；旧代次返回 False。"""
        existing = self._records.get(task_id)
        if existing is None or existing.generation != generation:
            return False
        self._records[task_id] = replace(
            existing,
            status=_coerce_status(status),
            result_json=existing.result_json if result_json is None else result_json,
            lease_expire_at=0.0,
        )
        return True

    async def lease(self, task_id: str, generation: int, lease_seconds: float) -> bool:
        """续租 / generation+1 重试；不允许回退 generation。"""
        existing = self._records.get(task_id)
        if existing is None or generation < existing.generation:
            return False
        if generation > existing.generation:
            # 更高代次接管：重置为 PLANNED 并清空旧结果，旧代次提交会被 fencing 拒绝。
            self._records[task_id] = replace(
                existing,
                generation=generation,
                status=TaskStatus.PLANNED,
                result_json="",
                lease_expire_at=time.time() + lease_seconds,
            )
        else:
            # 同代次续租：只延长到期时间，不动状态与结果。
            self._records[task_id] = replace(
                existing,
                lease_expire_at=time.time() + lease_seconds,
            )
        return True

    async def list_by_trace(
        self,
        trace_id: str,
        status: Optional[TaskStatus] = None,
    ) -> list[TaskRecord]:
        """按 trace_id 与可选状态过滤，按 task_id 升序返回副本。"""
        status_value = None if status is None else _status_value(status)
        records = [
            record
            for record in self._records.values()
            if record.trace_id == trace_id
            and (status_value is None or record.status.value == status_value)
        ]
        records.sort(key=lambda record: record.task_id)
        return [replace(record) for record in records]
