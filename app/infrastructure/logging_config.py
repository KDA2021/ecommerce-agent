# -*- coding: utf-8 -*-
"""P15-3 结构化日志：所有进程统一 JSON 行输出，便于采集 / 检索；可退回纯文本。

设计约束
--------
- **一份格式**：``ts`` / ``level`` / ``logger`` / ``msg`` 是固定四件套；
  有请求上下文时再补 ``request_id`` / ``trace_id``（没有就不输出这两个 key，
  而不是输出 null，日志检索时不会出现半空字段）。
- **JSON 可解析**：``json.dumps(ensure_ascii=False)``，中文原样保留；
  单行一条（``msg`` 里的换行被转义），适配 Loki / Fluent Bit 等按行采集。
- **text fallback**：``LOG_FORMAT=text``（大小写不敏感）时退回
  ``ts level logger [request_id=x trace_id=y] msg`` 的人类可读格式，
  本地直连终端排障不用先过 ``jq``。
- **级别**：``LOG_LEVEL``（默认 ``INFO``），非法值回落 ``INFO``，
  避免部署时拼错环境变量导致日志静默丢失。
- **stdout**：结构化日志的消费者是采集器而不是 tty，因此走 stdout
  （容器里 stdout / stderr 都会被收集，但 stdout 不被当作错误流）。

调用方式（进程入口调用一次即可，重复调用是幂等的）::

    from app.infrastructure.logging_config import configure_logging

    configure_logging()

之后各模块用标准库写法即可，无需传额外字段::

    import logging

    logger = logging.getLogger(__name__)
    logger.info("订单创建成功 order_id=%s", order_id)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

#: 默认根 logger 名称（本项目的库 / 应用日志都挂在它下面）。
DEFAULT_LOGGER_NAME = "app"

#: 结构化日志必需字段；缺任何一个都说明 formatter 与采集契约漂移了。
REQUIRED_FIELDS = ("ts", "level", "logger", "msg")

#: 可选上下文字段：仅在 ``ContextVar`` 里有非空值时才写入 JSON。
OPTIONAL_FIELDS = ("request_id", "trace_id")

#: 文本格式的时间戳形状（与既有 stdout 习惯一致，去掉毫秒）。
_TEXT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 标准库 ``LogRecord`` 自带的内部字段（args / exc_info / levelno ...）+ 格式化时才产生的
#: message / asctime。这些不进 JSON，只有 ``extra={...}`` 传进来的业务字段才透传。
_LOG_RECORD_INTERNALS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


def _is_extra_field(key: str) -> bool:
    """``extra=`` 字段是否属于可透传的业务字段（标准库内部字段 / 下划线开头一律丢弃）。"""
    return bool(key) and not key.startswith("_") and key not in _LOG_RECORD_INTERNALS


def _format_timestamp(created: float) -> str:
    """把 ``LogRecord.created``（epoch 秒）渲染成 ISO8601 UTC 毫秒时间戳。

    Args:
        created: ``time.time()`` 风格的 epoch 秒。

    Returns:
        形如 ``2026-10-15T12:00:00.123Z`` 的字符串（UTC），采集端可直接排序。
    """
    return (
        datetime.fromtimestamp(created, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _normalize_format(raw: Any) -> str:
    """把 ``LOG_FORMAT`` / 显式参数归一化为 ``"json"`` 或 ``"text"``。

    Args:
        raw: 原始配置值（环境变量或调用方传入）。

    Returns:
        ``"text"``（大小写 / 空白不敏感）或 ``"json"``（默认）。
    """
    value = str(raw or "").strip().lower()
    return "text" if value == "text" else "json"


def _normalize_level(raw: Any) -> int:
    """把 ``LOG_LEVEL`` 归一化为 logging 级别；非法值回落 ``INFO``。"""
    if isinstance(raw, int):
        return raw
    name = str(raw or "").strip().upper()
    level = logging.getLevelNamesMapping().get(name)
    return level if isinstance(level, int) else logging.INFO


class _RequestContext:
    """请求上下文快照（不可变）。

    Attributes:
        request_id: 单个 HTTP 请求的关联 ID。
        trace_id: 一轮对话的 trace ID；请求最早期的日志可能还没生成。
        path: 请求路径，仅用于 start / end 两条日志。
    """

    __slots__ = ("request_id", "trace_id", "path")

    def __init__(self, request_id: str = "", trace_id: str = "", path: str = "") -> None:
        self.request_id = request_id
        self.trace_id = trace_id
        self.path = path


#: 当前 asyncio Task 的请求上下文；``run_in_executor`` / ``create_task`` 会继承副本，
#: 因此后台任务里打的日志同样带上 request_id / trace_id。
_request_context: ContextVar[_RequestContext] = ContextVar(
    "app_request_log_context",
    default=_RequestContext(),
)


def current_request_id() -> str:
    """读取当前请求的 request_id（无上下文时为空串）。"""
    return _request_context.get().request_id


def current_trace_id() -> str:
    """读取当前请求的 trace_id；未显式绑定时回退 :class:`ShoppingContext`。

    用途：agent 主链路在拿到 trace_id 后写进 ``ShoppingContext``，
    这里回退保证「请求入口之后的日志」自动带上 trace_id，不必层层传参。

    Returns:
        trace_id；两处都没有时返回空串。
    """
    bound = _request_context.get().trace_id
    if bound:
        return bound
    try:
        from app.infrastructure.context import ShoppingContext
    except Exception:  # pragma: no cover - context 模块不可用时不该影响日志
        return ""
    return str(getattr(ShoppingContext, "current_trace_id", lambda: "")() or "")


def current_request_path() -> str:
    """读取当前请求的路径（仅供 start / end 日志使用，无上下文时为空串）。"""
    return _request_context.get().path


def set_request_context(
    request_id: str = "",
    trace_id: str = "",
    path: str = "",
) -> Any:
    """把请求上下文写进当前 Task，并返回给 ``reset_request_context`` 的 token。

    Args:
        request_id: 请求关联 ID。
        trace_id: 可选 trace ID。
        path: 可选请求路径。

    Returns:
        ``ContextVar`` token；必须在同一个 Task 里 ``reset``，避免串台。
    """
    return _request_context.set(
        _RequestContext(request_id=request_id, trace_id=trace_id, path=path),
    )


def reset_request_context(token: Any) -> None:
    """恢复 ``set_request_context`` 之前的上下文。"""
    _request_context.reset(token)


@contextmanager
def bind_request_context(
    request_id: str = "",
    trace_id: str = "",
    path: str = "",
) -> Iterator[None]:
    """以 ``with`` 语法临时绑定请求上下文，退出时自动恢复（中间件 / 测试用）。"""
    token = set_request_context(request_id=request_id, trace_id=trace_id, path=path)
    try:
        yield
    finally:
        reset_request_context(token)


class StructuredJsonFormatter(logging.Formatter):
    """把 ``LogRecord`` 序列化成单行 JSON。

    固定字段：``ts``（ISO8601，UTC，毫秒）/ ``level`` / ``logger`` / ``msg``；
    可选字段：``request_id`` / ``trace_id``（有值才输出）+ ``extra=`` 透传的业务字段。
    异常栈放在 ``exc_info`` 键里，保证采集端仍能定位堆栈。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def format(self, record: logging.LogRecord) -> str:
        """渲染一条日志。

        Args:
            record: 标准库日志记录。

        Returns:
            单行 JSON 字符串（不换行，UTF-8 中文原样保留）。
        """
        payload: dict[str, Any] = {
            "ts": _format_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        request_id = getattr(record, "request_id", "") or current_request_id()
        trace_id = getattr(record, "trace_id", "") or current_trace_id()
        if request_id:
            payload["request_id"] = str(request_id)
        if trace_id:
            payload["trace_id"] = str(trace_id)

        # ``extra=`` 透传的业务字段；固定字段与已写入的上下文字段优先，
        # 避免同名字段被覆盖成空值。
        payload_keys = set(payload) | set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
        for key, value in record.__dict__.items():
            if key in payload_keys or not _is_extra_field(key):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class RequestContextFilter(logging.Filter):
    """把上下文里的 request_id / trace_id 注入 ``LogRecord``。

    注入后 formatter 即使不读 ``ContextVar`` 也能拿到字段；同时方便测试用
    ``record.request_id`` 直接断言，而不必解析 JSON。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """写入缺失的上下文字段，恒返回 ``True``（不拦截任何日志）。"""
        if not getattr(record, "request_id", ""):
            record.request_id = current_request_id()
        if not getattr(record, "trace_id", ""):
            record.trace_id = current_trace_id()
        return True


class TextLogFormatter(logging.Formatter):
    """``LOG_FORMAT=text`` 的可读格式：``ts level logger [request_id=…] msg``。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(fmt="%(message)s", **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        """渲染人类可读单行文本。"""
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            _TEXT_TIMESTAMP_FORMAT,
        )
        parts = [stamp, record.levelname, record.name]
        request_id = getattr(record, "request_id", "") or current_request_id()
        trace_id = getattr(record, "trace_id", "") or current_trace_id()
        context = " ".join(
            f"{key}={value}"
            for key, value in (("request_id", request_id), ("trace_id", trace_id))
            if value
        )
        if context:
            parts.append(f"[{context}]")
        parts.append(record.getMessage())
        text = " ".join(parts)
        if record.exc_info:
            text = f"{text}\n{self.formatException(record.exc_info)}"
        return text


def build_formatter(log_format: str | None = None) -> logging.Formatter:
    """按 ``LOG_FORMAT`` 构造 formatter（显式参数优先，方便测试）。

    Args:
        log_format: ``"json"`` / ``"text"``；``None`` = 读环境变量 ``LOG_FORMAT``。

    Returns:
        :class:`StructuredJsonFormatter` 或 :class:`TextLogFormatter`。
    """
    effective = _normalize_format(os.getenv("LOG_FORMAT", "json") if log_format is None else log_format)
    if effective == "text":
        return TextLogFormatter()
    return StructuredJsonFormatter()


def _build_stdout_handler(
    log_format: str | None = None,
    log_level: str | int | None = None,
) -> logging.Handler:
    """构造带 formatter + 上下文过滤器、输出到 stdout 的 handler。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(log_format))
    handler.addFilter(RequestContextFilter())
    raw_level = os.getenv("LOG_LEVEL", "INFO") if log_level is None else log_level
    handler.setLevel(_normalize_level(raw_level))
    return handler


def configure_logging(
    log_format: str | None = None,
    level: str | int | None = None,
    logger_name: str = DEFAULT_LOGGER_NAME,
) -> logging.Logger:
    """装配进程级日志（幂等：重复调用只替换自己那个 handler）。

    为什么只挂 ``root``：

    - ``app`` 是本项目所有 ``getLogger(__name__)`` 的父 logger，级别挂这里；
    - 项目日志默认向上冒泡到 root，与第三方库（uvicorn / sqlalchemy / httpx）
      共用同一个 handler，容器里只会出现一种格式；只挂一处也天然不会重复打两行。
    - ``app`` 与 root 是两个 logger 对象：即便调用方手动跑了 ``basicConfig()``
      给 root 加了 handler，这里也只替换带本模块标记的 handler。

    幂等实现：只移除「本函数上次装的」handler（按私有标记识别），
    不动调用方 / 测试注入的 handler（例如 ``caplog``），也不会反复叠加。

    Args:
        log_format: 可选覆盖 ``LOG_FORMAT``。
        level: 可选覆盖 ``LOG_LEVEL``。
        logger_name: 业务根 logger 名称。

    Returns:
        业务根 logger（其 level 已按配置设置）。
    """
    effective_level = _normalize_level(os.getenv("LOG_LEVEL", "INFO") if level is None else level)
    business = logging.getLogger(logger_name)
    business.setLevel(effective_level)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_app_structured", False):
            root.removeHandler(handler)
    # 第三方库（uvicorn / sqlalchemy / httpx）大多只在 root 判定级别，
    # 这里与业务 logger 保持一致，容器里不会出现「app 有日志、库没日志」的割裂。
    root.setLevel(effective_level)

    handler = _build_stdout_handler(log_format)
    handler.setLevel(effective_level)
    handler._app_structured = True  # type: ignore[attr-defined] - 幂等标记
    root.addHandler(handler)
    return business
