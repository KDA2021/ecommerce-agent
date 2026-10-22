# -*- coding: utf-8 -*-
"""P10-4 前端体验补全回归：历史会话 / 空状态建议 / 骨架屏 / 重试 / i18n。

覆盖四层：

1. **前端纯函数**：子进程跑 `frontend/tests/experience.test.mts`（Node 原生 test runner），
   覆盖 i18n 字典与 `sessionHistory.ts` 的 localStorage 解析 / 后端合并 / thread 归一化；
2. **前端契约**：App.tsx / api.ts 源码里 P10-4 能力与 P10-1..3 既有能力都在；
3. **后端纯函数**：checkpointer 消息序列化、trace 回退解析、会话摘要；
4. **后端 API**：`GET /commerce/sessions` 与 `/commerce/sessions/{id}/history`
   在 checkpointer 可用 / 不可用 / 容器缺失时都返回可被前端消费的结构。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.presentation.sessions import (
    MAX_HISTORY_MESSAGES,
    _safe_session_id,
    read_trace_history,
    serialize_thread_messages,
    state_messages,
    summarize_trace_sessions,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
NODE_TEST_FILE = "tests/experience.test.mts"
NODE_ERROR_TEST_FILE = "tests/error-card.test.mts"


# --------------------------------------------------------------------------- #
# 1. 前端纯函数（Node 原生 test runner）
# --------------------------------------------------------------------------- #
def _node_executable() -> str | None:
    """返回可用的 node 可执行文件；缺失返回 None（CI 无 Node 时跳过）。"""
    return shutil.which("node")


def _run_node_tests(*relative_paths: str) -> str:
    """在 frontend/ 下跑一组 .mts 文件；缺 Node 跳过，文件缺失 / 失败则报错。"""
    node = _node_executable()
    if node is None:
        pytest.skip("未安装 Node，跳过前端纯函数单测")
    for relative_path in relative_paths:
        if not (FRONTEND / relative_path).exists():
            pytest.fail(f"前端测试文件缺失：{relative_path}")

    proc = subprocess.run(
        [node, "--test", *relative_paths],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, f"node --test 失败（exit={proc.returncode}）：\n{output}"
    # 防止测试文件被清空后"0 test passed"也返回 0。
    assert "pass " in output, output
    assert "fail 0" in output, output
    return output


def test_frontend_experience_pure_functions_pass_in_node_test_runner():
    """跑 frontend/tests/experience.test.mts，覆盖 i18n 与 sessionHistory。"""
    _run_node_tests(NODE_TEST_FILE)


def test_frontend_error_card_pure_functions_pass_in_node_test_runner():
    """跑 frontend/tests/error-card.test.mts，覆盖 P12-3 标记检测 / 重试契约。"""
    _run_node_tests(NODE_ERROR_TEST_FILE)


# --------------------------------------------------------------------------- #
# 2. 前端契约：i18n 条目数 / P10-4 能力 / P10-1..3 未回归
# --------------------------------------------------------------------------- #
def _dict_keys(source: str, name: str) -> set[str]:
    """从 i18n.ts 里抽出某个 dict 常量的 key 集合。"""
    marker = f"const {name}: Dict = {{"
    assert marker in source, f"i18n.ts 缺少 {name} 字典"
    block = source.split(marker, 1)[1].split("};", 1)[0]
    return set(re.findall(r'"([\w.]+)"\s*:', block))


def test_i18n_dictionary_at_least_15_entries_and_zh_en_parity():
    source = (FRONTEND / "src" / "i18n.ts").read_text(encoding="utf-8")
    zh = _dict_keys(source, "ZH")
    en = _dict_keys(source, "EN")
    assert len(zh) >= 15, f"i18n 字典只有 {len(zh)} 条，少于 15 条"
    assert zh == en, f"zh / en key 不一致：{sorted(zh ^ en)}"
    # 关键交互文案必须在字典里（语言切换后用户能看到变化）。
    for key in (
        "chat.send",
        "error.retry",
        "error.subagent",
        "error.subagentHint",
        "error.retrySubagent",
        "history.title",
        "lang.toggle",
    ):
        assert key in zh, f"i18n 缺少关键文案：{key}"


def test_frontend_app_keeps_p10_4_features():
    app_source = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    api_source = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    history_source = (FRONTEND / "src" / "sessionHistory.ts").read_text(encoding="utf-8")

    # 1) 历史会话：后端 thread 优先 + localStorage 兜底。
    assert "fetchSessionHistory" in app_source and "fetchSessionList" in app_source
    assert "loadThreadHistory" in app_source
    assert "/api/commerce/sessions" in api_source
    assert "/history" in api_source
    assert "SESSION_STORE_KEY" in app_source and "SESSION_STORE_KEY" in history_source
    assert "parseSessionStore" in history_source and "serializeSessionStore" in history_source
    assert "mergeBackendSessions" in history_source and "mergeHistory" in history_source
    assert 'data-testid="session-history"' in app_source

    # 2) 空状态 5 条建议问题。
    i18n_source = (FRONTEND / "src" / "i18n.ts").read_text(encoding="utf-8")
    assert "SUGGESTED_QUESTIONS" in i18n_source
    assert "suggestedQuestions" in app_source
    assert 'data-testid="suggestion"' in app_source

    # 3) 流式期间商品骨架屏。
    assert "ProductSkeleton" in app_source
    assert 'data-testid="product-skeleton"' in app_source

    # 4) SSE / 请求错误重试按钮。
    assert 'data-testid="retry-button"' in app_source
    assert "source.onerror" in app_source
    assert "retryLastError" in app_source

    # 5) i18n 语言切换。
    assert "nextLang" in app_source and 'data-testid="lang-toggle"' in app_source


def test_frontend_renders_subagent_error_card_with_same_session_retry():
    """P12-3：final.result / AIMessage 含 [subagent unavailable] → 错误卡片 + 重试。"""
    app_source = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    api_source = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    i18n_source = (FRONTEND / "src" / "i18n.ts").read_text(encoding="utf-8")

    # 1) 检测标记：常量与后端 subagents.SUBAGENT_UNAVAILABLE 逐字一致，且是纯函数。
    assert "[subagent unavailable]" in api_source
    assert "SUBAGENT_UNAVAILABLE_MARKER" in api_source
    assert "detectSubagentFailure" in api_source and "detectSubagentFailure" in app_source
    # AIMessage 恢复路径（历史消息内容）也要检测。
    assert "detectSubagentFailure(message.content)" in app_source

    # 2) final.result 与 direct REST 两条路径都经过统一落库 + 检测。
    assert '"final.result"' in app_source
    assert app_source.count("applyAssistantReply(") >= 3  # 定义 + runIntent + onFinalResult

    # 3) partial 结果照常显示：正文原样落库，错误卡片只补充提示。
    assert 'updateAssistant("replace", reply, meta)' in app_source
    assert 'data-testid="error-card"' in app_source
    assert "SubagentErrorCard" in app_source

    # 4) 重试重发当前 query、session id 不变；重试不重复插入用户消息。
    assert "retrySubagent" in app_source
    assert "retryLastError" in app_source
    assert "shopping_session_id: sessionId" in app_source
    assert "runIntent(target, { retry: true })" in app_source

    # 5) i18n 错误 / 重试文案齐备。
    for key in ("error.subagent", "error.subagentHint", "error.retrySubagent"):
        assert key in i18n_source, f"i18n 缺少 P12-3 文案：{key}"


def test_p10_1_2_3_features_still_present():
    """P10-4 不得把反馈 / 下单确认 / 可解释推荐 / Dashboard 弄丢。"""
    app_source = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    for token in (
        "submitFeedback",
        "FEEDBACK_REASONS",
        "OrderCardView",
        "findResultForTrace",
        "PreferenceChips",
        "buildComparisonRows",
        "ComparisonTable",
        "preference_hint",
        "/dashboard",
    ):
        assert token in app_source, f"App.tsx 丢失 P10 既有能力：{token}"
    assert "Dashboard" in app_source
    dashboard_source = (FRONTEND / "src" / "Dashboard.tsx").read_text(encoding="utf-8")
    assert "getDashboard" in dashboard_source


# --------------------------------------------------------------------------- #
# 3. 后端纯函数
# --------------------------------------------------------------------------- #
def test_serialize_thread_messages_keeps_only_visible_turns():
    messages = [
        HumanMessage(content="帮我找旅行三件套"),
        AIMessage(
            content="",
            tool_calls=[{"name": "product_search", "args": {}, "id": "c1", "type": "tool_call"}],
        ),
        ToolMessage(content="命中 3 个商品", tool_call_id="c1"),
        AIMessage(content="推荐这三款"),
        HumanMessage(content="   "),
    ]
    serialized = serialize_thread_messages(messages)
    assert serialized == [
        {"role": "user", "content": "帮我找旅行三件套"},
        {"role": "assistant", "content": "推荐这三款"},
    ]


def test_serialize_thread_messages_accepts_dicts_and_caps_limit():
    messages = [{"role": "user", "content": f"q{i}"} for i in range(5)]
    messages += [{"role": "tool", "content": "should-be-dropped"}]
    capped = serialize_thread_messages(messages, limit=2)
    assert capped == [{"role": "user", "content": "q3"}, {"role": "user", "content": "q4"}]
    assert len(serialize_thread_messages(messages)) == 5
    assert len(serialize_thread_messages(messages, limit=MAX_HISTORY_MESSAGES)) == 5


def test_state_messages_reads_values_from_snapshot():
    snapshot = SimpleNamespace(values={"messages": [HumanMessage(content="hi")], "other": 1})
    assert state_messages(snapshot) == [HumanMessage(content="hi")]
    assert state_messages(SimpleNamespace(values=None)) == []
    assert state_messages(object()) == []


def test_safe_session_id_blocks_path_traversal():
    safe = _safe_session_id("../../etc/passwd")
    assert "/" not in safe and "\\" not in safe and ".." not in safe
    assert _safe_session_id("s-1") == "s-1"
    assert _safe_session_id("") == "anonymous"


def test_read_trace_history_returns_only_final_replies(tmp_path: Path):
    trace = tmp_path / "s-trace.jsonl"
    lines = [
        {"type": "token.delta", "payload": {"token": "你"}},
        {"type": "final.result", "payload": {"reply": "第一轮回复"}},
        {"type": "error", "payload": {"message": "boom"}},
        {"type": "final.result", "payload": {"reply": "第二轮回复"}},
        "not-json",
    ]
    trace.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
    assert read_trace_history(tmp_path, "s-trace") == [
        {"role": "assistant", "content": "第一轮回复"},
        {"role": "assistant", "content": "第二轮回复"},
    ]
    assert read_trace_history(tmp_path, "missing") == []


def test_summarize_trace_sessions_counts_turns_and_sorts(tmp_path: Path):
    (tmp_path / "s-a.jsonl").write_text(
        "\n".join(
            json.dumps(line, ensure_ascii=False)
            for line in [
                {"type": "final.result", "payload": {"reply": "A1"}},
                {"type": "final.result", "payload": {"reply": "A2"}},
                {"type": "error", "payload": {"message": "x"}},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "s-b.jsonl").write_text(
        json.dumps({"type": "final.result", "payload": {"reply": "B1"}}),
        encoding="utf-8",
    )
    # 拉开 mtime，保证排序确定。
    import os
    import time

    now = time.time()
    os.utime(tmp_path / "s-a.jsonl", (now - 100, now - 100))
    os.utime(tmp_path / "s-b.jsonl", (now, now))

    sessions = summarize_trace_sessions(tmp_path)
    assert [item["session_id"] for item in sessions] == ["s-b", "s-a"]
    assert sessions[1]["turns"] == 2
    assert sessions[1]["errors"] == 1
    assert sessions[1]["has_history"] is True
    assert sessions[1]["preview"] == "A2"
    assert summarize_trace_sessions(tmp_path / "missing") == []


# --------------------------------------------------------------------------- #
# 4. 后端 API：checkpointer 优先 → trace 兜底 → empty
# --------------------------------------------------------------------------- #
class FakeAgent:
    """checkpointer 替身：aget_state 返回带 LangChain 消息的快照。"""

    def __init__(self, messages: list | None = None, boom: bool = False) -> None:
        self.messages = list(messages or [])
        self.boom = boom
        self.calls: list = []

    async def aget_state(self, config):
        self.calls.append(config)
        if self.boom:
            raise RuntimeError("checkpointer down")
        return SimpleNamespace(values={"messages": list(self.messages)})


def _client(agent: object | None, data_dir: Path) -> TestClient:
    from app.presentation.server import create_app

    container = SimpleNamespace(agent=agent, settings=SimpleNamespace(data_dir=data_dir))
    return TestClient(create_app(container=container, build_on_startup=False))


def test_history_api_prefers_checkpointer(tmp_path: Path):
    agent = FakeAgent([HumanMessage(content="问题"), AIMessage(content="回答")])
    client = _client(agent, tmp_path)
    response = client.get("/commerce/sessions/s-1/history")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s-1"
    assert body["source"] == "checkpointer"
    assert body["messages"] == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
    assert agent.calls == [{"configurable": {"thread_id": "s-1"}}]


def test_history_api_falls_back_to_trace_when_checkpointer_fails(tmp_path: Path):
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "s-2.jsonl").write_text(
        json.dumps({"type": "final.result", "payload": {"reply": "trace 里的回复"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    client = _client(FakeAgent(boom=True), tmp_path)
    response = client.get("/commerce/sessions/s-2/history")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "trace"
    assert body["messages"] == [{"role": "assistant", "content": "trace 里的回复"}]


def test_history_api_returns_empty_without_container():
    from app.presentation.server import create_app

    client = TestClient(create_app(container=None, build_on_startup=False))
    response = client.get("/commerce/sessions/s-3/history")
    assert response.status_code == 200
    assert response.json() == {"session_id": "s-3", "source": "empty", "messages": []}


def test_sessions_list_api_scans_trace_dir(tmp_path: Path):
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "s-9.jsonl").write_text(
        json.dumps({"type": "final.result", "payload": {"reply": "你好"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    client = _client(FakeAgent(), tmp_path)
    response = client.get("/commerce/sessions")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    item = body["sessions"][0]
    assert item["session_id"] == "s-9"
    assert item["turns"] == 1
    assert item["has_history"] is True
    assert item["preview"] == "你好"
    assert isinstance(item["updated_at"], float)


def test_sessions_list_without_container_is_empty():
    from app.presentation.server import create_app

    client = TestClient(create_app(container=None, build_on_startup=False))
    response = client.get("/commerce/sessions")
    assert response.status_code == 200
    # 没有容器 settings 时回退项目 data/traces；只断言结构，不依赖本地是否有 trace。
    assert set(response.json()) == {"sessions", "count"}
    assert isinstance(response.json()["sessions"], list)
