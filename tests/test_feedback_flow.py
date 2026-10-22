# -*- coding: utf-8 -*-
"""P10-1 反馈闭环单测：反馈表/API、badcase 回流、Dashboard 指标聚合。

测试分三层：
1. 纯函数：reason 枚举校验、trace/反馈聚合、cost 估算（不连库）；
2. API：用内存假 store 跑 POST/GET /commerce/feedback 与 /commerce/dashboard；
3. 集成：TEST_MYSQL_URL 存在时跑真实 MySQL 反馈 CRUD（未设置则自动跳过）。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain.feedback.feedback import (
    REASON_LABELS,
    UserFeedback,
    normalize_reason,
    reason_label,
)
from app.infrastructure.persistence.badcase_store import JsonlBadcaseWriter
from app.presentation.dashboard import (
    aggregate_dashboard_metrics,
    estimate_cost,
    estimate_tokens,
    summarize_feedback,
    summarize_traces,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "eval_regression.py"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeFeedbackStore:
    """内存版 FeedbackStore：给 API 测试用，不碰 MySQL。"""

    def __init__(self) -> None:
        self.items: list[UserFeedback] = []
        self._next = 0

    async def add(self, feedback: UserFeedback) -> int:
        self._next += 1
        self.items.append(replace(feedback, id=self._next))
        return self._next

    async def list_by_session(self, session_id: str, limit: int = 100) -> list[UserFeedback]:
        rows = [item for item in self.items if item.session_id == session_id]
        return rows[:limit]

    async def list_all(self, limit: int | None = None) -> list[UserFeedback]:
        return list(self.items if limit is None else self.items[:limit])


def _client(store: object | None = None, writer: object | None = None, data_dir: Path | None = None) -> TestClient:
    from app.presentation.server import create_app

    container = SimpleNamespace(
        feedback_store=store,
        badcase_store=writer,
        settings=SimpleNamespace(data_dir=data_dir or ROOT / "data"),
        engine=None,
    )
    return TestClient(create_app(container=container, build_on_startup=False))


def _payload(**overrides: object) -> dict:
    body = {
        "session_id": "s-feedback",
        "score": -1,
        "query": "推荐一个旅行三件套",
        "trace_id": "trace-1",
        "reply_preview": "抱歉，没有找到合适商品",
        "reason": "product_irrelevant",
        "buyer_id": "buyer-1",
    }
    body.update(overrides)
    return body


def _write_trace(trace_dir: Path, session_id: str, lines: list[dict]) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def _load_eval_module():
    """用文件路径加载 scripts/eval_regression.py（scripts 不是包）。"""
    spec = importlib.util.spec_from_file_location("eval_regression_feedback", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def eval_module():
    return _load_eval_module()


# --------------------------------------------------------------------------- #
# 1. 值对象 / 枚举校验
# --------------------------------------------------------------------------- #
def test_normalize_reason_accepts_code_and_chinese_label():
    assert normalize_reason("price_mismatch") == "price_mismatch"
    assert normalize_reason("价格不符合") == "price_mismatch"
    assert normalize_reason("") == ""
    assert reason_label("price_mismatch") == "价格不符合"
    assert reason_label("unknown") == "unknown"


def test_normalize_reason_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_reason("随便写的理由")


def test_user_feedback_validates_score_and_dislike_reason():
    assert UserFeedback(session_id="s1", score=1).reason == ""
    assert UserFeedback(session_id="s1", score=1, reason="价格不符合").reason == "price_mismatch"

    with pytest.raises(ValueError):
        UserFeedback(session_id="s1", score=0)
    with pytest.raises(ValueError):
        UserFeedback(session_id="s1", score=-1, reason="")
    with pytest.raises(ValueError):
        UserFeedback(session_id="s1", score=-1, reason="不存在的枚举")


def test_user_feedback_truncates_reply_preview_and_serializes():
    feedback = UserFeedback(
        session_id="s1",
        score=-1,
        reason="too_slow",
        reply_preview="x" * 900,
    )
    assert len(feedback.reply_preview) == 500
    payload = feedback.to_dict()
    assert payload["reason"] == "too_slow"
    assert payload["reason_label"] == "回答太慢"
    assert payload["score"] == -1
    assert len(REASON_LABELS) == 5


# --------------------------------------------------------------------------- #
# 2. 反馈 API（内存 store）
# --------------------------------------------------------------------------- #
def test_feedback_api_roundtrip_like_and_dislike():
    store = FakeFeedbackStore()
    client = _client(store=store)

    liked = client.post("/commerce/feedback", json=_payload(score=1, reason=""))
    assert liked.status_code == 200
    assert liked.json()["status"] == "ok"

    disliked = client.post("/commerce/feedback", json=_payload(score=-1, reason="price_mismatch"))
    assert disliked.status_code == 200
    assert disliked.json()["reason_label"] == "价格不符合"

    listed = client.get("/commerce/feedback", params={"session_id": "s-feedback"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 2
    assert [item["score"] for item in body["items"]] == [1, -1]
    assert body["items"][1]["query"] == "推荐一个旅行三件套"
    assert body["items"][1]["trace_id"] == "trace-1"
    # GET 只回该会话的反馈
    assert client.get("/commerce/feedback", params={"session_id": "other"}).json()["count"] == 0


def test_feedback_api_rejects_invalid_reason_enum():
    client = _client(store=FakeFeedbackStore())
    response = client.post("/commerce/feedback", json=_payload(reason="随便写的理由"))
    assert response.status_code == 422
    assert "reason" in response.json()["detail"]


def test_feedback_api_requires_reason_for_dislike():
    client = _client(store=FakeFeedbackStore())
    response = client.post("/commerce/feedback", json=_payload(score=-1, reason=""))
    assert response.status_code == 422


def test_feedback_api_requires_store():
    from app.presentation.server import create_app

    client = TestClient(create_app(container=None, build_on_startup=False))
    response = client.post("/commerce/feedback", json=_payload())
    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# 3. badcase 回流
# --------------------------------------------------------------------------- #
def test_dislike_writes_badcase_jsonl(tmp_path):
    path = tmp_path / "badcases.jsonl"
    writer = JsonlBadcaseWriter(path)
    store = FakeFeedbackStore()
    client = _client(store=store, writer=writer, data_dir=tmp_path)

    dislike = client.post("/commerce/feedback", json=_payload(score=-1, reason="too_slow"))
    assert dislike.status_code == 200

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    record = lines[0]
    assert record["session_id"] == "s-feedback"
    assert record["query"] == "推荐一个旅行三件套"
    assert record["score"] == -1
    assert record["reason"] == "too_slow"
    assert record["reason_label"] == "回答太慢"
    assert record["kind"] == "general"
    assert record["id"].startswith("badcase-")

    # 点赞不写 badcase
    assert client.post("/commerce/feedback", json=_payload(score=1, reason="")).status_code == 200
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_badcase_writer_swallows_write_errors(tmp_path):
    """目标路径不可写时 append 返回 False，不向上抛。"""
    target = tmp_path / "as_dir" / "badcases.jsonl"
    target.mkdir(parents=True)
    writer = JsonlBadcaseWriter(target)
    assert writer.append_nowait(UserFeedback(session_id="s", score=-1, reason="other")) is False


# --------------------------------------------------------------------------- #
# 4. Dashboard 指标聚合（纯函数）
# --------------------------------------------------------------------------- #
def _sample_trace_dir(tmp_path: Path) -> Path:
    trace_dir = tmp_path / "traces"
    _write_trace(
        trace_dir,
        "s-a",
        [
            {"type": "token.delta", "trace_id": "t1", "payload": {"token": "你好"}},
            {"type": "token.delta", "trace_id": "t1", "payload": {"token": "世界"}},
            {"type": "span", "trace_id": "t1", "name": "ttft", "duration_ms": 120.0},
            {"type": "span", "trace_id": "t1", "name": "main_agent", "duration_ms": 900.0},
            {"type": "final.result", "trace_id": "t1", "payload": {"reply": "你好世界"}},
        ],
    )
    _write_trace(
        trace_dir,
        "s-b",
        [
            {"type": "cache.hit", "trace_id": "t2", "payload": {"query": "旅行"}},
            {"type": "final.result", "trace_id": "t2", "payload": {"reply": "缓存回复"}},
        ],
    )
    return trace_dir


def test_summarize_traces_counts_sessions_turns_ttft_and_cache(tmp_path):
    stats = summarize_traces(_sample_trace_dir(tmp_path))
    assert stats["sessions"] == 2
    assert stats["completed_sessions"] == 2
    assert stats["turns"] == 2
    assert stats["cache_hits"] == 1
    assert stats["ttft_ms"] == 120.0
    assert stats["ttft_samples"] == 1
    assert stats["llm_calls"] == 1
    assert stats["total_tokens_estimate"] == estimate_tokens(4)


def test_summarize_feedback_distribution():
    likes = UserFeedback(session_id="s", score=1)
    dislike = UserFeedback(session_id="s", score=-1, reason="price_mismatch")
    stats = summarize_feedback([likes, dislike, dislike])
    assert stats["likes"] == 1
    assert stats["dislikes"] == 2
    assert stats["reason_distribution"]["price_mismatch"] == 2
    assert stats["reason_distribution"]["too_slow"] == 0


def test_estimate_cost_is_token_price_based():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(3) == 2
    assert estimate_cost(1000, 0.002) == 0.002
    assert estimate_cost(500, 0.004) == 0.002


def test_aggregate_dashboard_metrics_combines_three_sources(tmp_path):
    trace_dir = _sample_trace_dir(tmp_path)
    feedback = [
        UserFeedback(session_id="s-a", score=1),
        UserFeedback(session_id="s-a", score=-1, reason="product_irrelevant"),
        UserFeedback(session_id="s-b", score=-1, reason="too_slow"),
    ]
    metrics = aggregate_dashboard_metrics(
        trace_dir, feedback, order_success=3, price_per_1k_tokens=0.002,
    )
    assert metrics["sessions"] == 2
    assert metrics["turns"] == 2
    assert metrics["avg_turns"] == 1.0
    assert metrics["completion_rate"] == 1.0
    assert metrics["likes"] == 1
    assert metrics["dislikes"] == 2
    assert metrics["reason_distribution"]["product_irrelevant"] == 1
    assert metrics["reason_distribution"]["too_slow"] == 1
    assert metrics["ttft_ms"] == 120.0
    assert metrics["cache_hits"] == 1
    assert metrics["cache_hit_rate"] == 0.5
    assert metrics["order_success"] == 3
    assert metrics["total_tokens_estimate"] == estimate_tokens(4)
    assert metrics["cost_estimate"] == estimate_cost(estimate_tokens(4), 0.002)


def test_dashboard_api_endpoint_returns_real_aggregates(tmp_path):
    store = FakeFeedbackStore()
    client = _client(store=store, data_dir=tmp_path)
    assert client.post("/commerce/feedback", json=_payload(score=-1, reason="wrong_info")).status_code == 200

    response = client.get("/commerce/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["dislikes"] == 1
    assert body["reason_distribution"]["wrong_info"] == 1
    assert body["sessions"] == 1
    assert body["turns"] == 0
    assert "cost_estimate" in body


# --------------------------------------------------------------------------- #
# 5. eval_regression --with-badcases
# --------------------------------------------------------------------------- #
def test_load_badcases_converts_jsonl_and_skips_dirty_lines(eval_module, tmp_path):
    path = tmp_path / "badcases.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "badcase-t1",
                        "session_id": "s1",
                        "query": "为什么没有货",
                        "buyer_id": "buyer-9",
                        "reason": "wrong_info",
                        "reply_preview": "错误回复",
                    },
                    ensure_ascii=False,
                ),
                "",
                "not-json",
                json.dumps({"id": "no-query", "query": "   "}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cases = eval_module.load_badcases(path)
    assert len(cases) == 1
    assert cases[0]["id"] == "badcase-t1"
    assert cases[0]["query"] == "为什么没有货"
    assert cases[0]["buyer_id"] == "buyer-9"
    assert cases[0]["kind"] == "general"
    assert cases[0]["must_contain"] == []
    assert cases[0]["must_not_contain"] == []
    assert cases[0]["source"] == "badcase"


def test_load_badcases_missing_file_returns_empty(eval_module, tmp_path):
    assert eval_module.load_badcases(tmp_path / "nope.jsonl") == []


def test_merge_cases_appends_and_renames_collisions(eval_module):
    yaml_cases = [
        {
            "id": "x",
            "kind": "general",
            "query": "q1",
            "buyer_id": "b1",
            "must_contain": [],
            "must_not_contain": [],
        }
    ]
    badcases = [
        {"id": "x", "kind": "general", "query": "q2", "buyer_id": "b2", "must_contain": [], "must_not_contain": []},
        {"id": "y", "kind": "general", "query": "q3", "buyer_id": "b3", "must_contain": [], "must_not_contain": []},
    ]
    merged = eval_module.merge_cases(yaml_cases, badcases)
    assert [case["id"] for case in merged] == ["x", "x-2", "y"]


def test_run_eval_exposes_with_badcases(eval_module):
    parameters = inspect.signature(eval_module.run_eval).parameters
    assert "with_badcases" in parameters
    assert eval_module.BADCASES_PATH.name == "badcases.jsonl"


# --------------------------------------------------------------------------- #
# 6. MySQL 集成（TEST_MYSQL_URL 未设置时跳过）
# --------------------------------------------------------------------------- #
pytestmark_mysql = pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"), reason="TEST_MYSQL_URL 未设置，跳过 MySQL 反馈测试"
)


@pytest.fixture()
async def mysql_feedback_store():
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine
    from app.infrastructure.persistence.mysql_feedback_store import MySqlFeedbackStore

    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    await bootstrap_schema(engine)
    store = MySqlFeedbackStore(engine)
    yield store
    await engine.dispose()


@pytestmark_mysql
async def test_mysql_feedback_crud(mysql_feedback_store):
    session_id = f"s-mysql-fb-{uuid.uuid4().hex[:8]}"
    feedback = UserFeedback(
        session_id=session_id,
        score=-1,
        query="推荐旅行三件套",
        trace_id="trace-mysql",
        reply_preview="没有找到",
        reason="价格不符合",
        buyer_id="buyer-mysql",
    )
    feedback_id = await mysql_feedback_store.add(feedback)
    assert feedback_id > 0

    rows = await mysql_feedback_store.list_by_session(session_id)
    assert len(rows) == 1
    assert rows[0].id == feedback_id
    assert rows[0].reason == "price_mismatch"
    assert rows[0].reason_text == "价格不符合"
    assert rows[0].trace_id == "trace-mysql"

    all_rows = await mysql_feedback_store.list_all()
    assert any(row.id == feedback_id for row in all_rows)

    assert await mysql_feedback_store.delete(feedback_id) is True
    assert await mysql_feedback_store.list_by_session(session_id) == []
    # 幂等：重复删除返回 False
    assert await mysql_feedback_store.delete(feedback_id) is False
