"""Cost controls must not hide unknown products or reuse stale judgments."""
import copy
import json
from types import SimpleNamespace

import pytest


def catalog():
    return {f"P{i}": {"product_id": f"P{i}", "title": f"Product {i}",
                      "brand": "Shared" if i < 3 else f"Brand{i}", "price": "89",
                      "currency": "CNY", "aliases": [f"Model{i}"],
                      "description": "Fact " * 200,
                      "sku_prices": [{"amount": "99", "currency": "CNY"}]}
            for i in range(1, 5)}


def raw_extraction():
    return json.dumps({"recommendations": [], "claims": [],
                       **{k: {"value": False, "evidence": ""} for k in
                          ("no_match", "clarification", "acknowledgement")}})


def test_projection_keeps_every_identity_and_all_mentioned_facts():
    from scripts.judge_efficiency import project_catalog
    source = catalog()
    original = copy.deepcopy(source)
    result = project_catalog(source, {"query": "P1", "history": []}, "Unknown P999; Model3")
    assert {p["product_id"] for p in result} == set(source)
    by_id = {p["product_id"]: p for p in result}
    assert by_id["P1"] == source["P1"]
    assert by_id["P3"] == source["P3"]
    assert "description" not in by_id["P4"]
    assert by_id["P4"]["sku_prices"] == source["P4"]["sku_prices"]
    assert source == original


def test_shared_brand_and_history_preserve_every_potentially_relevant_product():
    from scripts.judge_efficiency import project_catalog
    source = catalog()
    result = project_catalog(source, {"query": "compare", "history": [{"content": "Shared"}]}, "Brand4")
    assert all("description" in p for p in result if p["product_id"] in ("P1", "P2", "P4"))


def test_compact_prompt_is_blind_and_keeps_unknown_claims_observable():
    from scripts.shopping_grading import judge_prompt
    source = catalog()
    task = {"query": "Model1", "expectation": {"secret": "answer"}, "description": "answer key"}
    compact = judge_prompt(task, "P999 costs 10元", source, catalog_mode="compact")
    full = judge_prompt(task, "P999 costs 10元", source)
    payload = json.loads(compact[1]["content"])
    assert payload["task"] == {"query": "Model1"}
    assert payload["reply"] == "P999 costs 10元"
    assert len(payload["catalog"]) == 4
    assert len(json.dumps(compact)) < len(json.dumps(full))
    with pytest.raises(ValueError):
        judge_prompt(task, "reply", source, catalog_mode="invalid")


@pytest.mark.asyncio
async def test_cached_judge_reuses_extraction_but_model_or_prompt_changes_miss(tmp_path):
    from scripts.judge_efficiency import CachedJudge
    class Judge:
        calls = 0
        async def ainvoke(self, prompt, **kwargs):
            self.calls += 1
            return SimpleNamespace(content=raw_extraction())
    judge = Judge()
    prompt = [{"role": "user", "content": "original"}]
    first = CachedJudge(judge, tmp_path, {"model": "a"})
    await first.ainvoke(prompt)
    second = CachedJudge(judge, tmp_path, {"model": "a"})
    assert (await second.ainvoke(prompt)).content == raw_extraction()
    assert judge.calls == 1
    assert second.stats()["hits"] == 1
    await CachedJudge(judge, tmp_path, {"model": "b"}).ainvoke(prompt)
    await second.ainvoke([{"role": "user", "content": "changed"}])
    assert judge.calls == 3


@pytest.mark.asyncio
async def test_malformed_or_failed_judge_responses_are_never_cached(tmp_path):
    from scripts.judge_efficiency import CachedJudge
    class Judge:
        calls = 0
        async def ainvoke(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(content="not json")
            raise RuntimeError("provider down")
    judge = Judge()
    cached = CachedJudge(judge, tmp_path, {"model": "a"})
    await cached.ainvoke([])
    with pytest.raises(RuntimeError):
        await cached.ainvoke([])
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_corrupt_cache_falls_back_without_accepting_wrong_response(tmp_path):
    from scripts.judge_efficiency import CachedJudge
    class Judge:
        calls = 0
        async def ainvoke(self, prompt, **kwargs):
            self.calls += 1
            return SimpleNamespace(content=raw_extraction())
    judge = Judge()
    cached = CachedJudge(judge, tmp_path, {"model": "a"})
    await cached.ainvoke([])
    next(tmp_path.glob("*.json")).write_text("corrupt", encoding="utf-8")
    await cached.ainvoke([])
    assert judge.calls == 2
    assert cached.stats()["corrupt"] == 1


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_discard_valid_model_output(tmp_path):
    from scripts.judge_efficiency import CachedJudge
    class Judge:
        async def ainvoke(self, *args, **kwargs):
            return SimpleNamespace(content=raw_extraction())
    blocked = tmp_path / "file-not-directory"
    blocked.write_text("existing", encoding="utf-8")
    judge = CachedJudge(Judge(), blocked, {"model": "test"})
    assert (await judge.ainvoke([])).content == raw_extraction()
    assert judge.stats()["write_errors"] == 1


@pytest.mark.asyncio
async def test_runner_cache_hit_still_rechecks_changed_task_constraints(tmp_path):
    from langchain_core.messages import AIMessage

    from scripts.eval_shopping import run_trajectory
    from scripts.judge_efficiency import CachedJudge
    class Agent:
        async def ainvoke(self, *args, **kwargs):
            return {"messages": [AIMessage(content="Product 1 89元")]}
    class Preferences:
        async def list_by_buyer(self, buyer):
            return []
    class Judge:
        calls = 0
        async def ainvoke(self, prompt, **kwargs):
            self.calls += 1
            value = json.loads(raw_extraction())
            value["recommendations"] = [{"product_id": "P1", "evidence": "Product 1 89元"}]
            return SimpleNamespace(content=json.dumps(value))
    raw = Judge()
    judge = CachedJudge(raw, tmp_path / "cache", {"model": "test"})
    container = SimpleNamespace(agent=Agent(), preference_store=Preferences(),
                                data_policy=SimpleNamespace(sanitize_output=lambda s: s),
                                settings=SimpleNamespace(data_dir=tmp_path))
    task = {"id": "same", "category": "test", "turns": [{"query": "recommend", "expectation":
            {"mode": "recommend", "eligible_ids": ["P1"]}}]}
    first = await run_trajectory(container, judge, task, {"route": "direct", "repetition": 1}, catalog(), "run", [])
    task["turns"][0]["expectation"]["eligible_ids"] = ["P2"]
    second = await run_trajectory(container, judge, task, {"route": "direct", "repetition": 2}, catalog(), "run", [])
    assert first["task_passed"] is True
    assert second["task_passed"] is False
    assert raw.calls == 1
    assert first["turns"][0]["judge_cache"]["misses"] == 1
    assert second["turns"][0]["judge_cache"]["hits"] == 1


def test_summary_reports_cache_reuse_separately_from_agent_calls():
    from scripts.eval_shopping import summarize_runs
    row = {"task_id": "a", "route": "direct", "repetition": 1, "category": "test",
           "latency_seconds": 1, "llm_calls": 3, "task_passed": True, "error": "",
           "turns": [{"judge_cache": {"hits": 1, "misses": 0}}]}
    arm = summarize_runs([row])["arms"]["direct"]
    assert arm["judge_cache_hits"] == 1
    assert arm["judge_cache_misses"] == 0
    assert arm["llm_calls_mean"] == 3
