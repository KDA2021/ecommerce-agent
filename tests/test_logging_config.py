# -*- coding: utf-8 -*-
"""P15-3 结构化日志测试：JSON 字段契约、request_id/trace_id、text fallback、请求中间件。

测试不打真实 stdout：每个用例把 root handler 换成写入 ``StringIO`` 的同款
formatter / filter（由 fixture 负责临时替换并恢复），因此可以直接断言「日志行里
到底是什么」，同时不污染其他测试的日志配置。
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient

from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.logging_config import (
    REQUIRED_FIELDS,
    StructuredJsonFormatter,
    TextLogFormatter,
    bind_request_context,
    build_formatter,
    configure_logging,
    current_request_id,
    current_trace_id,
)
from app.presentation.server import create_app

#: 本模块的日志捕获器名称（fixture 临时挂到 root 上）。
CAPTURE_LOGGER_NAME = "app.test_capture"


class _CaptureHandler(logging.StreamHandler):
    """写入内存 buffer 的 handler；持有 stream 而不是替换 ``sys.stdout``。"""

    def __init__(self, buffer: StringIO, formatter: logging.Formatter) -> None:
        super().__init__(buffer)
        self.setFormatter(formatter)


def _build_capture_handler(buffer: StringIO, log_format: str = "json") -> _CaptureHandler:
    """构造与生产同款的 handler（同一 formatter + 同一上下文过滤器）。"""
    from app.infrastructure.logging_config import RequestContextFilter

    handler = _CaptureHandler(buffer, build_formatter(log_format))
    handler.addFilter(RequestContextFilter())
    return handler


@pytest.fixture
def log_buffer():
    """把 root 的 handler 临时替换成内存捕获器，测完恢复原状。

    同时把 root / ``app`` 级别降到 DEBUG，保证被断言的两条 http 日志一定落盘；
    用 ``try/finally`` 恢复，避免影响同进程的其他测试。
    """
    buffer = StringIO()
    handler = _build_capture_handler(buffer)
    root = logging.getLogger()
    app_logger = logging.getLogger("app")
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_app_level = app_logger.level

    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    app_logger.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        root.handlers = saved_root_handlers
        root.setLevel(saved_root_level)
        app_logger.setLevel(saved_app_level)


def _log_lines(buffer: StringIO) -> list[dict]:
    """把 buffer 里的非空行全部解析成 JSON dict。"""
    lines = []
    for raw in buffer.getvalue().splitlines():
        if raw.strip():
            lines.append(json.loads(raw))
    return lines


def _lines_for(buffer: StringIO, logger_name: str) -> list[dict]:
    """只取某个 logger 名下的日志行。"""
    return [line for line in _log_lines(buffer) if line.get("logger") == logger_name]


# --------------------------------------------------------------------------- #
# JSON 契约
# --------------------------------------------------------------------------- #
def test_json_log_has_required_base_fields(log_buffer):
    """固定四件套 ts/level/logger/msg 必须在，且 ts 为 ISO8601 UTC。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    logger.info("下单成功 order_id=%s", "o-1")

    payload = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)[0]

    assert set(REQUIRED_FIELDS) <= set(payload)
    assert payload["level"] == "INFO"
    assert payload["logger"] == CAPTURE_LOGGER_NAME
    assert payload["msg"] == "下单成功 order_id=o-1"
    assert payload["ts"].endswith("Z")
    assert "T" in payload["ts"]


def test_json_log_omits_context_fields_when_unbound(log_buffer):
    """没有请求上下文时不输出 request_id / trace_id（而不是输出 null）。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    logger.info("无上下文")

    payload = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)[0]

    assert "request_id" not in payload
    assert "trace_id" not in payload
    assert current_request_id() == ""
    assert current_trace_id() == ""


def test_json_log_includes_request_and_trace_id_when_bound(log_buffer):
    """绑定上下文后，所有日志自动带上 request_id / trace_id（无需调用方传参）。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    with bind_request_context("req-42", trace_id="trace-42"):
        logger.info("带上下文")

    payload = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)[0]

    assert payload["request_id"] == "req-42"
    assert payload["trace_id"] == "trace-42"


def test_context_is_reset_after_binding_block(log_buffer):
    """``with`` 退出后上下文必须复原，否则并发请求会互相串台。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    with bind_request_context("req-scoped"):
        logger.info("block 内")
    logger.info("block 外")

    inside, outside = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)
    assert inside["request_id"] == "req-scoped"
    assert "request_id" not in outside


def test_trace_id_falls_back_to_shopping_context(log_buffer):
    """agent 主链路把 trace_id 写进 ``ShoppingContext`` 后，日志应自动带上。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    snapshot = ShoppingContextSnapshot(
        shopping_session_id="s-1",
        buyer_id="b-1",
        locale="zh-CN",
        currency="CNY",
        trace_id="trace-from-shop-context",
    )
    token = ShoppingContext.set(snapshot)
    try:
        logger.info("主链路之后")
    finally:
        ShoppingContext.reset(token)

    payload = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)[0]

    assert payload["trace_id"] == "trace-from-shop-context"


def test_extra_fields_are_forwarded(log_buffer):
    """``extra={...}`` 的业务字段（如 order_id / status）原样进入 JSON。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    logger.info("事件", extra={"order_id": "o-9", "status": 200})

    payload = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)[0]

    assert payload["order_id"] == "o-9"
    assert payload["status"] == 200
    # 标准库内部字段不应泄漏进日志
    assert "args" not in payload
    assert "levelno" not in payload


def test_utf8_is_not_escaped(log_buffer):
    """``ensure_ascii=False``：中文原样输出，采集端不必二次解码转义。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    logger.info("中文消息")

    raw_line = log_buffer.getvalue().strip()

    assert "中文消息" in raw_line
    assert "\\u" not in raw_line


def test_exception_log_includes_traceback(log_buffer):
    """异常日志保留堆栈（``exc_info`` 字段），排障信息不丢。"""
    logger = logging.getLogger(CAPTURE_LOGGER_NAME)
    try:
        raise ValueError("boom-1")
    except ValueError:
        logger.exception("炸了")

    payload = _lines_for(log_buffer, CAPTURE_LOGGER_NAME)[0]

    assert payload["level"] == "ERROR"
    assert payload["msg"] == "炸了"
    assert "ValueError: boom-1" in payload["exc_info"]
    assert "Traceback" in payload["exc_info"]


# --------------------------------------------------------------------------- #
# LOG_FORMAT=text fallback
# --------------------------------------------------------------------------- #
def test_text_formatter_emits_readable_line():
    """``LOG_FORMAT=text`` 退回人类可读单行，而不是 JSON。"""
    record = logging.LogRecord(
        name="app.demo",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    text = TextLogFormatter().format(record)

    assert "app.demo" in text
    assert "hello world" in text
    assert "INFO" in text
    assert not text.startswith("{")


def test_text_formatter_shows_request_id():
    """text 模式下 request_id / trace_id 也保留（排障时按 ID grep）。"""
    record = logging.LogRecord("app.demo", logging.INFO, __file__, 1, "msg", None, None)
    with bind_request_context("req-text", trace_id="trace-text"):
        text = TextLogFormatter().format(record)

    assert "request_id=req-text" in text
    assert "trace_id=trace-text" in text


def test_build_formatter_honors_log_format_env(monkeypatch):
    """``LOG_FORMAT`` 大小写不敏感；除 text 外一律 JSON。"""
    monkeypatch.setenv("LOG_FORMAT", "TEXT")
    assert isinstance(build_formatter(), TextLogFormatter)

    monkeypatch.setenv("LOG_FORMAT", "json")
    assert isinstance(build_formatter(), StructuredJsonFormatter)

    monkeypatch.setenv("LOG_FORMAT", "随便写")
    assert isinstance(build_formatter(), StructuredJsonFormatter)

    monkeypatch.delenv("LOG_FORMAT", raising=False)
    assert isinstance(build_formatter(), StructuredJsonFormatter)


def test_text_log_line_is_not_json():
    """capture handler 也可以切成 text，验证端到端不是 JSON。"""
    buffer = StringIO()
    handler = _build_capture_handler(buffer, log_format="text")
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    try:
        logging.getLogger(CAPTURE_LOGGER_NAME).info("文本一行")
        raw = buffer.getvalue().strip()
    finally:
        root.handlers = saved
        root.setLevel(saved_level)

    assert "文本一行" in raw
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


# --------------------------------------------------------------------------- #
# configure_logging
# --------------------------------------------------------------------------- #
def test_configure_logging_is_idempotent(monkeypatch):
    """重复调用不叠加 handler（否则每条日志会打多份）。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure_logging()
        configure_logging()
        configure_logging()
        tagged = [h for h in root.handlers if getattr(h, "_app_structured", False)]
        assert len(tagged) == 1
        assert isinstance(tagged[0].formatter, StructuredJsonFormatter)
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_respects_level(monkeypatch):
    """``LOG_LEVEL`` 生效；非法值回落 INFO 而不是静默丢日志。"""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    logger = configure_logging()
    assert logger.level == logging.WARNING

    monkeypatch.setenv("LOG_LEVEL", "not-a-level")
    assert configure_logging().level == logging.INFO


# --------------------------------------------------------------------------- #
# 请求中间件：request_id 注入 + start / end 日志
# --------------------------------------------------------------------------- #
SERVER_LOGGER = "app.presentation.server"


def test_middleware_generates_request_id_and_logs_start_end(log_buffer):
    """没有 X-Request-ID 时生成一个，start / end 两条日志共享同一个 ID。"""
    client = TestClient(create_app(container=None, build_on_startup=False))
    response = client.get("/health")

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id

    entries = _lines_for(log_buffer, SERVER_LOGGER)
    starts = [line for line in entries if line.get("event") == "http.request.start"]
    ends = [line for line in entries if line.get("event") == "http.request.end"]

    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["request_id"] == request_id
    assert ends[0]["request_id"] == request_id
    assert ends[0]["status"] == 200
    assert ends[0]["path"] == "/health"
    assert ends[0]["method"] == "GET"
    assert ends[0]["duration_ms"] >= 0


def test_middleware_reuses_incoming_request_id(log_buffer):
    """合法 ``X-Request-ID`` 原样透传（网关 / 前端串联用），并写回响应头。"""
    client = TestClient(create_app(container=None, build_on_startup=False))
    response = client.get("/health", headers={"X-Request-ID": "gw-abc-123"})

    assert response.headers["X-Request-ID"] == "gw-abc-123"
    entries = _lines_for(log_buffer, SERVER_LOGGER)
    assert {line["request_id"] for line in entries} == {"gw-abc-123"}


def test_middleware_rejects_unsafe_request_id(log_buffer):
    """带非法字符的头一律丢弃并重新生成，避免日志 / 响应头注入。"""
    client = TestClient(create_app(container=None, build_on_startup=False))
    response = client.get("/health", headers={"X-Request-ID": "bad id!\n"})

    request_id = response.headers["X-Request-ID"]
    assert request_id != "bad id!\n"
    assert len(request_id) == 32  # uuid4().hex
    entries = _lines_for(log_buffer, SERVER_LOGGER)
    assert {line["request_id"] for line in entries} == {request_id}


def test_effective_request_id_boundaries():
    """边界口径：合法头透传，空 / 超长 / 非法字符一律丢弃。"""
    from starlette.datastructures import Headers

    from app.presentation.dependencies import MAX_REQUEST_ID_LENGTH, effective_request_id

    generated = "generated-id"
    valid = "a" * MAX_REQUEST_ID_LENGTH

    assert effective_request_id(Headers({}), generated) == generated
    assert effective_request_id(Headers({"X-Request-ID": "  "}), generated) == generated
    assert effective_request_id(Headers({"X-Request-ID": valid}), generated) == valid
    assert (
        effective_request_id(Headers({"X-Request-ID": "a" * (MAX_REQUEST_ID_LENGTH + 1)}), generated)
        == generated
    )
    assert effective_request_id(Headers({"X-Request-ID": "bad id"}), generated) == generated
    assert effective_request_id(Headers({"X-Request-ID": "trace:1.2-3_4"}), generated) == "trace:1.2-3_4"


def test_middleware_logs_unhandled_exception(log_buffer):
    """路由未捕获异常时，end 位置记 error 日志（带堆栈）后原样抛出。

    异常路径由 FastAPI 的异常处理链生成 500，错误日志里的 ``request_id``
    是排障入口（响应头在异常分支不保证存在，这里不做假设）。
    """
    app = create_app(container=None, build_on_startup=False)

    @app.get("/boom")
    async def boom() -> dict:  # pragma: no cover - 由 TestClient 触发
        raise RuntimeError("middleware-boom")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom", headers={"X-Request-ID": "err-req-1"})

    assert response.status_code == 500

    entries = _lines_for(log_buffer, SERVER_LOGGER)
    errors = [line for line in entries if line.get("event") == "http.request.error"]

    assert len(errors) == 1
    assert errors[0]["level"] == "ERROR"
    assert errors[0]["request_id"] == "err-req-1"
    assert errors[0]["path"] == "/boom"
    assert "middleware-boom" in errors[0]["exc_info"]


def test_middleware_binds_request_state_for_dependency_injection(log_buffer):
    """中间件写入的 ``request.state.request_id`` 能被路由 / 依赖读到。"""
    from app.presentation.dependencies import get_request_id

    app = create_app(container=None, build_on_startup=False)

    @app.get("/probe")
    async def probe(
        request: Request,
        injected: str = Depends(get_request_id),
    ) -> dict:
        return {
            "state": getattr(request.state, "request_id", ""),
            "injected": injected,
        }

    client = TestClient(app)
    response = client.get("/probe", headers={"X-Request-ID": "probe-req-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "probe-req-1"
    assert body["injected"] == "probe-req-1"
    assert response.headers["X-Request-ID"] == "probe-req-1"

    entries = _lines_for(log_buffer, SERVER_LOGGER)
    assert entries
    assert all(line["request_id"] == "probe-req-1" for line in entries)
