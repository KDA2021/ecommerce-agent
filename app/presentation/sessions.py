# -*- coding: utf-8 -*-
"""P10-4 会话历史 API：``GET /commerce/sessions`` + ``/{session_id}/history``。

前端优先读后端 LangGraph thread（checkpointer）里的真实消息，读不到再回退
localStorage；这两个接口就是"后端 thread 查询"的落点。设计约束：

- **只读**：不改任何对话状态，不写库；
- **fail-safe**：checkpointer / trace 读取异常时返回空消息 + ``source=empty``，
  不把 500 抛给前端，让前端能静默回退本地记录；
- **不编造**：响应里的每个字段都来自 trace 文件或 checkpointer，缺就缺。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

#: 单次历史查询最多返回的消息条数（保留最新的）。
MAX_HISTORY_MESSAGES = 50

#: 会话列表最多返回的条数（按最近活跃倒序）。
MAX_SESSION_SUMMARIES = 50

#: LangChain 消息 type → 前端展示角色；其它（tool / system）不对外暴露。
_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
}


def _safe_session_id(session_id: str) -> str:
    """与 tracing 同口径清洗 session_id，避免路径穿越。

    Args:
        session_id: 会话 id。

    Returns:
        可安全拼进 trace 文件名的字符串。
    """
    name = str(session_id).replace("\\", "_").replace("/", "_").replace("..", "_")
    return name or "anonymous"


def _field(message: object, key: str):
    """同时兼容 LangChain 消息对象与普通 dict 取值。"""
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _content_text(content: object) -> str:
    """把 LangChain 的 str / content-block list 归一化成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def serialize_thread_messages(messages: object, limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """把 checkpointer 里的消息序列化成前端可渲染的 ``[{role, content}]``。

    只保留有文本的 user / assistant 消息；tool / system / 空内容全部丢弃，
    并按 ``limit`` 保留最新的若干条。

    Args:
        messages: 消息可迭代对象。
        limit: 最多返回条数。

    Returns:
        ``[{"role": "user"|"assistant", "content": str}]``。
    """
    out: list[dict] = []
    for message in messages or []:
        role = _ROLE_MAP.get(str(_field(message, "type") or _field(message, "role") or "").lower())
        if role is None:
            continue
        content = _content_text(_field(message, "content"))
        if not content.strip():
            continue
        out.append({"role": role, "content": content})
    if limit and limit > 0:
        return out[-limit:]
    return out


def state_messages(snapshot: object) -> list:
    """从 LangGraph ``aget_state`` 快照里取出 ``messages`` 列表。"""
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict):
        return []
    messages = values.get("messages")
    return list(messages) if messages else []


def read_trace_history(trace_dir: str | Path, session_id: str) -> list[dict]:
    """回退路径：从 ``{trace_dir}/{session_id}.jsonl`` 里取每轮 ``final.result``。

    trace 未落用户问题（只落 token / 工具 / final 事件），所以这里只能还原
    assistant 的回复，**不会伪造用户消息**。

    Args:
        trace_dir: trace 目录。
        session_id: 会话 id。

    Returns:
        ``[{"role": "assistant", "content": str}]``。
    """
    path = Path(trace_dir) / f"{_safe_session_id(session_id)}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    messages: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(line, dict) or line.get("type") != "final.result":
            continue
        payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
        reply = payload.get("reply")
        if isinstance(reply, str) and reply.strip():
            messages.append({"role": "assistant", "content": reply})
    return messages[-MAX_HISTORY_MESSAGES:]


def summarize_trace_sessions(trace_dir: str | Path) -> list[dict]:
    """扫描 trace 目录，列出会话 + 轮数 + 最近活跃时间 + 最近回复预览。

    Args:
        trace_dir: trace 目录。

    Returns:
        按最近活跃倒序的会话摘要列表。
    """
    trace_dir = Path(trace_dir)
    if not trace_dir.is_dir():
        return []
    sessions: list[dict] = []
    for path in sorted(trace_dir.glob("*.jsonl")):
        turns = 0
        errors = 0
        last_reply = ""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            mtime = path.stat().st_mtime
        except OSError:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(line, dict):
                continue
            event_type = str(line.get("type", ""))
            if event_type == "final.result":
                turns += 1
                payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
                reply = payload.get("reply")
                if isinstance(reply, str) and reply.strip():
                    last_reply = reply
            elif event_type == "error":
                errors += 1
        preview = last_reply.replace("\n", " ").strip()
        sessions.append(
            {
                "session_id": path.stem,
                "turns": turns,
                "errors": errors,
                "updated_at": mtime,
                "has_history": turns > 0,
                "preview": preview[:60],
            }
        )
    sessions.sort(key=lambda item: item["updated_at"], reverse=True)
    return sessions[:MAX_SESSION_SUMMARIES]


def _trace_dir(container) -> Path:
    """从容器 settings 推导 trace 目录（缺省回退项目 ``data/traces``）。"""
    settings = getattr(container, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        from app.infrastructure.settings import PROJECT_ROOT

        data_dir = PROJECT_ROOT / "data"
    return Path(data_dir) / "traces"


async def collect_thread_history(container, session_id: str) -> tuple[str, list[dict]]:
    """按"checkpointer 优先，trace 兜底"取会话历史。

    Args:
        container: 装配容器或测试替身。
        session_id: 会话 id。

    Returns:
        ``(source, messages)``；source ∈ checkpointer / trace / empty。
    """
    agent = getattr(container, "agent", None) if container is not None else None
    aget_state = getattr(agent, "aget_state", None)
    if callable(aget_state):
        try:
            snapshot = await aget_state({"configurable": {"thread_id": session_id}})
            messages = serialize_thread_messages(state_messages(snapshot))
            if messages:
                return "checkpointer", messages
        except Exception:  # noqa: BLE001 - checkpointer 故障不能阻断前端回退
            pass

    trace_messages = read_trace_history(_trace_dir(container), session_id)
    if trace_messages:
        return "trace", trace_messages
    return "empty", []


def build_sessions_router() -> APIRouter:
    """兼容旧路径：会话历史路由已迁到 ``app.presentation.routers.sessions``。

    本模块保留 checkpointer / trace 读取与序列化纯函数供测试与 router 复用，
    仅路由器构造转发到 routers 包。

    Returns:
        APIRouter。
    """
    from app.presentation.routers.sessions import build_sessions_router as _build_router

    return _build_router()
