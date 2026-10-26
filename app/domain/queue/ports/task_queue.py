# -*- coding: utf-8 -*-
"""TaskQueue 端口 + 任务值对象

削峰用的任务队列抽象。当前仓库只定义端口与值对象，未接入运行时（计划中/P8）；
换 RabbitMQ / Kafka 只需另写一个实现，application 与 domain 不动。

投递语义按 at-least-once 设计：消息重投、worker 崩溃重启都会造成
同一任务被消费两次，因此调用方必须自己保证幂等——create_order 是写操作，
重复消费等于重复下单。当前只是端口契约，尚未接入生产者/消费者。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    """返回当前 UTC ISO 时间串。

    Returns:
        ISO 时间串。
    """
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IntentTask:
    """意图任务值对象，供削峰队列投递。"""

    task_id: str
    shopping_session_id: str
    buyer_id: str
    locale: str
    currency: str
    raw_query: str
    enqueued_at: str = field(default_factory=_now_iso)
    # 优先级：0 = 正常请求（1 以上 = 大请求，走低优先队列）。
    # 长会话（轮数多、上下文大）单次耗时明显更长，
    # 把它们分到单独队列，避免把短平快的新会话堵在后面。
    priority: int = 0
    # P11-4：worker 侧要还原的额外请求上下文（如 A/B 实验开关快照）。
    # 默认空 dict，保证既有构造调用不受影响。
    experiment_config: dict = field(default_factory=dict)
    client_request_id: str = ""

    def to_dict(self) -> dict:
        """序列化成可入队结构。

        Returns:
            任务 dict。
        """
        return {
            "task_id": self.task_id,
            "shopping_session_id": self.shopping_session_id,
            "buyer_id": self.buyer_id,
            "locale": self.locale,
            "currency": self.currency,
            "raw_query": self.raw_query,
            "enqueued_at": self.enqueued_at,
            "priority": self.priority,
            "experiment_config": dict(self.experiment_config or {}),
            "client_request_id": self.client_request_id,
        }

    @staticmethod
    def from_dict(raw: dict) -> "IntentTask":
        """从入队结构还原任务。

        Args:
            raw: 任务 dict。

        Returns:
            IntentTask。
        """
        config = raw.get("experiment_config") or {}
        if isinstance(config, str):
            # Redis Stream field 都是字符串，JSON 串要还原成 dict。
            try:
                import json

                config = json.loads(config)
            except Exception:
                config = {}
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        return IntentTask(
            task_id=raw["task_id"],
            shopping_session_id=raw["shopping_session_id"],
            buyer_id=raw.get("buyer_id", ""),
            locale=raw.get("locale", "zh-CN"),
            currency=raw.get("currency", "CNY"),
            raw_query=raw.get("raw_query", ""),
            enqueued_at=raw.get("enqueued_at", ""),
            priority=priority,
            experiment_config=dict(config) if isinstance(config, dict) else {},
            client_request_id=raw.get("client_request_id", ""),
        )


@dataclass(frozen=True)
class TaskStatus:
    """任务状态值对象：state 为 queued / running / done / failed。"""

    task_id: str
    state: str  # queued / running / done / failed
    final_text: str = ""
    error: str = ""
    queue_position: int = 0


class TaskQueue(ABC):
    """任务队列端口（at-least-once，调用方需自行幂等）。"""

    @abstractmethod
    async def enqueue(self, task: IntentTask) -> object:
        """投递任务。

        返回值留给实现方做可观测性（例如 Redis Stream 的 message id）；
        调用方不应依赖其具体类型（P11-4 之前约定为 ``None``）。

        Args:
            task: 意图任务。
        """
        ...

    @abstractmethod
    async def set_status(self, status: TaskStatus) -> None:
        """写回任务状态。

        Args:
            status: 任务状态。
        """
        ...

    @abstractmethod
    async def get_status(self, task_id: str) -> Optional[TaskStatus]:
        """查询任务状态。

        Args:
            task_id: 任务 id。

        Returns:
            TaskStatus；不存在返回 None。
        """
        ...

    @abstractmethod
    async def depth(self) -> int:
        """待消费任务数，供 /health 与容量观察使用。

        Returns:
            当前待消费任务数。
        """
