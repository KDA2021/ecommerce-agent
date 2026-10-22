# -*- coding: utf-8 -*-
import inspect
import os
import threading

import pytest

from app.infrastructure.rerank.bge_reranker import BgeReranker


class _FakeModel:
    """记录 predict 调用的测试替身，绝不触发真实权重加载。"""

    def __init__(self) -> None:
        self.calls: list[list[list[str]]] = []

    def predict(self, pairs):
        self.calls.append(pairs)
        return [0.5 for _ in pairs]


async def test_empty_documents_returns_empty():
    reranker = BgeReranker()
    assert await reranker.rerank("query", []) == []


def test_warmup_is_public_async():
    assert inspect.iscoroutinefunction(BgeReranker.warmup)


async def test_warmup_loads_model_off_event_loop(monkeypatch):
    """warmup 必须在工作线程加载权重，而不是同步阻塞事件循环线程。"""
    reranker = BgeReranker()
    event_loop_thread = threading.get_ident()
    loader_threads: list[int] = []
    fake_model = _FakeModel()

    def _fake_ensure_model():
        loader_threads.append(threading.get_ident())
        reranker._model = fake_model
        return fake_model

    monkeypatch.setattr(reranker, "_ensure_model", _fake_ensure_model)

    loaded = await reranker.warmup()

    assert loaded is fake_model
    assert len(loader_threads) == 1
    assert loader_threads[0] != event_loop_thread


async def test_rerank_routes_through_warmup(monkeypatch):
    """rerank 只 await warmup()，不再同步调用 _ensure_model 冷加载。"""
    reranker = BgeReranker()
    fake_model = _FakeModel()
    warmup_calls: list[bool] = []

    async def _fake_warmup():
        warmup_calls.append(True)
        return fake_model

    def _boom():
        raise AssertionError("rerank 不得同步调用 _ensure_model")

    monkeypatch.setattr(reranker, "warmup", _fake_warmup)
    monkeypatch.setattr(reranker, "_ensure_model", _boom)

    scores = await reranker.rerank("q", ["a", "b"])

    assert warmup_calls == [True]
    assert scores == [0.5, 0.5]
    assert fake_model.calls == [[["q", "a"], ["q", "b"]]]


@pytest.mark.skipif(
    os.getenv("RERANKER_INTEGRATION") != "1",
    reason="RERANKER_INTEGRATION=1 时运行真实模型测试",
)
async def test_real_model_scores_order():
    reranker = BgeReranker("BAAI/bge-reranker-base")
    docs = [
        "Nomadica 旅行三件套 抗造 轻便 无塑料",
        "速干毛巾 游泳 健身",
    ]
    scores = await reranker.rerank("旅行三件套 抗造", docs)
    assert len(scores) == 2
    assert scores[0] > scores[1]
