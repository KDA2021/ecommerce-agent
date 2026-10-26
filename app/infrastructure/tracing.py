# -*- coding: utf-8 -*-
"""本地 JSONL trace：把一次请求产生的事件按 session 落盘，便于离线回放/排障。

不接 LangSmith 等外部可观测服务，只写 ``{data_dir}/traces/{session_id}.jsonl``，
每行一条 ``{"ts", "trace_id", "type", "payload"}``，与 SSE 帧一一对应。

P8 在事件 trace 之上增加 span：``span(name, attrs=None)`` 是异步上下文管理器，
自动维护 ``contextvars`` 父子栈，退出时追加一行 span JSONL。

query / reply / token 等会话文本字段只落 ``sha256[:16] + 长度``；凭据、收货信息和
可识别敏感值同样递归脱敏。事件 payload 与 span attrs 都走同一策略，普通工具名、
状态和数值指标保持可读。脱敏作用于副本，不改变 SSE 的源 payload。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.application.data_policy import TRACE_TEXT_FIELDS, DataPolicy, text_fingerprint
from app.application.metrics.session_metrics import record_span as record_metric_span
from app.infrastructure.context import ShoppingContext


def _safe_session_id(session_id: str) -> str:
    """session_id 来自请求体，去掉路径分隔/上跳片段，避免写出 traces 目录之外。"""
    name = str(session_id).replace("\\", "_").replace("/", "_").replace("..", "_")
    return name or "anonymous"


#: 需要脱敏的字段名（大小写不敏感）：这些字段的值属于用户会话文本。
#: 其余字段（tool / status / span / order 等结构化信息）原样保留。
REDACTED_FIELDS = TRACE_TEXT_FIELDS


def redact_text(text: str) -> dict:
    """把敏感文本替换成 ``{"sha256": 前 16 位, "length": 原文长度}``。

    Args:
        text: 待脱敏的原始文本。

    Returns:
        脱敏标记 dict；哈希基于 UTF-8 编码，长度按字符数统计。
    """
    return text_fingerprint(text)


def redact_sensitive(value: Any, data_policy=None) -> Any:
    """用独立 trace 策略递归脱敏 payload / attrs。

    会话文本、凭据及可识别敏感值替换为指纹；dict 层级、list 顺序及普通结构化
    字段保持不变。始终返回新对象，绝不原地修改调用方字典——同一个 payload
    还要投递给 SSE 客户端，不能被 trace 脱敏污染。

    Args:
        value: 任意 JSON 兼容结构（payload / attrs）。

    Returns:
        脱敏后的新结构。
    """
    policy = data_policy if data_policy is not None else DataPolicy()
    return policy.redact_trace(value)


@dataclass
class SpanHandle:
    """span 上下文里可读写的轻量句柄；调用方可标记 status / 补充 attrs。"""

    span_id: str                                # 本 span 唯一 id
    name: str                                   # span 名称，如 planner / rerank
    attrs: dict = field(default_factory=dict)   # 自定义属性（token 数、候选数等）
    status: str = "ok"                          # ok / error
    started_at: float = 0.0                     # 开始时间（wall clock）
    ended_at: float = 0.0                       # 结束时间（wall clock）
    duration_ms: float = 0.0                    # 耗时（毫秒，用 perf_counter 计算）

    def set_status(self, status: str) -> None:
        """标记 span 状态；非法值忽略，避免污染 JSONL。

        Args:
            status: ``ok`` 或 ``error``。
        """
        if status in ("ok", "error"):
            self.status = status


@asynccontextmanager
async def traced_span(tracer, name: str, attrs: Optional[dict] = None):
    """可选 tracer 的统一入口：``None`` 时是零开销 no-op。

    Args:
        tracer: LocalTraceLogger 或拥有 ``span`` 异步上下文管理器的对象。
        name: span 名称。
        attrs: 可选属性。

    Yields:
        tracer 的 span 句柄或 ``None``。
    """
    if tracer is None:
        yield None
        return
    async with tracer.span(name, attrs) as handle:
        yield handle


class LocalTraceLogger:
    """按 session 追加写入 JSONL 的极简 trace logger。

    ``record`` 是异步接口（供 async 调用方 await）；``record_nowait`` 是同步桥接，
    给 ``EventBroker.publish`` 这类同步方法用。落盘失败只吞掉异常，绝不阻断主流程。
    """

    def __init__(self, data_dir: str | Path, data_policy=None) -> None:
        """初始化 trace 目录。

        Args:
            data_dir: 数据根目录。
        """
        self.data_dir = Path(data_dir)
        self.data_policy = data_policy if data_policy is not None else DataPolicy()
        self.trace_dir = self.data_dir / "traces"
        # 用 ContextVar 而非全局 list：父子 span 栈在并发协程间天然隔离。
        self._span_stack: ContextVar[list[str]] = ContextVar(
            f"local_trace_span_stack_{id(self)}",
            default=[],
        )

    def path_for(self, session_id: str) -> Path:
        """返回某 session 的 JSONL 路径（session_id 已做路径清洗）。

        Args:
            session_id: 会话 id。

        Returns:
            Path。
        """
        return self.trace_dir / f"{_safe_session_id(session_id)}.jsonl"

    @staticmethod
    def build_line(trace_id: str, event_type: str, payload: dict, data_policy=None) -> dict:
        """构造一行 trace：ts / trace_id / type / payload（payload 已脱敏）。

        Args:
            trace_id: 请求级 trace id。
            event_type: 事件类型。
            payload: 事件载荷。

        Returns:
            trace dict。
        """
        return {
            "ts": time.time(),
            "trace_id": trace_id,
            "type": event_type,
            "payload": redact_sensitive(payload, data_policy),
        }

    @asynccontextmanager
    async def span(self, name: str, attrs: Optional[dict] = None):
        """记录一段耗时 span，退出时追加一条 JSONL。

        当前 session / trace_id 从 ``ShoppingContext`` 读取；父子关系由实例级
        ``ContextVar[list[str]]`` 自动维护，退出/异常都会恢复父栈。

        Args:
            name: span 名称。
            attrs: 可选属性 dict。

        Yields:
            SpanHandle: 可在上下文内标记状态或补充 attrs。
        """
        span_id = uuid.uuid4().hex
        session_id = ShoppingContext.current_session_id()
        trace_id = ShoppingContext.current_trace_id()
        parent_stack = self._span_stack.get()
        parent_span_id = parent_stack[-1] if parent_stack else None
        token = self._span_stack.set([*parent_stack, span_id])
        started_at = time.time()
        started_perf = time.perf_counter()
        handle = SpanHandle(
            span_id=span_id,
            name=name,
            attrs=dict(attrs or {}),
            started_at=started_at,
        )
        # P10-5：LLM 节点 span 计入当前请求的 llm_calls（未设置采集器时 no-op）。
        record_metric_span(name)
        try:
            yield handle
        except BaseException as exc:  # noqa: BLE001 - 必须标 error 后原样抛出
            handle.status = "error"
            handle.attrs.setdefault("error", str(exc))
            raise
        finally:
            # 无论成功/异常都恢复父栈，避免并发时 span 树串线。
            self._span_stack.reset(token)
            handle.ended_at = time.time()
            handle.duration_ms = (time.perf_counter() - started_perf) * 1000.0
            line = {
                "type": "span",
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "name": name,
                "duration_ms": handle.duration_ms,
                "status": handle.status if handle.status in ("ok", "error") else "ok",
                "attrs": redact_sensitive(handle.attrs, self.data_policy),
                "started_at": handle.started_at,
                "ended_at": handle.ended_at,
            }
            try:
                await asyncio.to_thread(self._append, self.path_for(session_id), line)
            except Exception:  # noqa: BLE001 - trace 是旁路能力，失败不能影响业务
                pass

    async def record(self, session_id: str, trace_id: str, event_type: str, payload: dict) -> None:
        """异步追加一条 trace（线程池落盘，避免阻塞事件循环）。

        Args:
            session_id: 会话 id。
            trace_id: 请求级 trace id。
            event_type: 事件类型。
            payload: 事件载荷。
        """
        line = self.build_line(trace_id, event_type, payload, self.data_policy)
        await asyncio.to_thread(self._append, self.path_for(session_id), line)

    def record_nowait(self, session_id: str, trace_id: str, event_type: str, payload: dict) -> None:
        """同步追加一条 trace；任何落盘/序列化异常都不向上抛。

        Args:
            session_id: 会话 id。
            trace_id: 请求级 trace id。
            event_type: 事件类型。
            payload: 事件载荷。
        """
        try:
            self._append(
                self.path_for(session_id),
                self.build_line(trace_id, event_type, payload, self.data_policy),
            )
        except Exception:  # noqa: BLE001 - trace 是旁路能力，失败不能影响业务
            pass

    @staticmethod
    def _append(path: Path, line: dict) -> None:
        """追加写入一行 JSON（自动建目录）。

        Args:
            path: 目标文件。
            line: trace dict。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
