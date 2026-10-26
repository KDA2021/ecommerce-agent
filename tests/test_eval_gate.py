from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import eval_regression as evaluation


def order(**changes):
    result = dict(order_id="GBX-000001", buyer_id="buyer", status="CONFIRMED",
                  lines=[SimpleNamespace(sku_id="P1001-S1", quantity=1)],
                  total_amount=lambda: SimpleNamespace(amount_in_minor_units=18900, currency="CNY"))
    result.update(changes)
    return SimpleNamespace(**result)


def expectation():
    return {"action": "create", "items": [{"sku_id": "P1001-S1", "quantity": 1}],
            "amount_minor": 18900, "currency": "CNY", "status": "CONFIRMED"}


def test_order_check_accepts_correct_new_persisted_order():
    assert evaluation.check_order_postconditions(expectation(), "buyer", {"GBX-000001": order()},
                                               set(), {"GBX-000001"}, ["GBX-000001"]) == []


@pytest.mark.parametrize("changes,expected", [
    ({"buyer_id": "other"}, "buyer"),
    ({"status": "CANCELLED"}, "status"),
    ({"lines": [SimpleNamespace(sku_id="P1001-S2", quantity=1)]}, "items"),
    ({"lines": [SimpleNamespace(sku_id="P1001-S1", quantity=2)]}, "items"),
    ({"total_amount": lambda: SimpleNamespace(amount_in_minor_units=1, currency="CNY")}, "amount"),
])
def test_order_check_rejects_wrong_persisted_business_fields(changes, expected):
    failures = evaluation.check_order_postconditions(expectation(), "buyer", {"GBX-000001": order(**changes)},
                                                    set(), {"GBX-000001"}, ["GBX-000001"])
    assert any(expected in failure for failure in failures)


def test_preexisting_order_cannot_satisfy_creation_even_when_fields_match():
    assert evaluation.check_order_postconditions(expectation(), "buyer", {"GBX-000001": order()},
                                                {"GBX-000001"}, set(), ["GBX-000001"])


def test_unreported_or_duplicate_new_orders_fail():
    for ids, mentions in [({"GBX-000001"}, []), ({"GBX-000001", "GBX-000002"}, ["GBX-000001"])]:
        assert evaluation.check_order_postconditions(expectation(), "buyer", {"GBX-000001": order()},
                                                    set(), ids, mentions)


def test_missing_order_query_requires_persisted_absence_and_no_new_order():
    expected = {"action": "absent", "order_id": "GBX-999999"}
    assert evaluation.check_order_postconditions(expected, "buyer", {"GBX-999999": None}, set(), set(), []) == []
    assert evaluation.check_order_postconditions(expected, "buyer", {"GBX-999999": order()}, set(), set(), [])
    assert evaluation.check_order_postconditions(expected, "buyer", {"GBX-999999": None}, set(), {"GBX-1"}, [])


def test_business_failure_overrides_perfect_judge_and_gate_fails():
    result = evaluation.finalize_result({"score": 2, "error": "", "business_failures": ["buyer mismatch"]})
    assert result["judge_score"] == 2
    assert result["score"] == 0 and result["task_passed"] is False
    summary = evaluation.summarize_results([result], min_score=0.8, min_pass_rate=1.0)
    assert summary["exit_code"] == 1 and summary["normalized_score"] == 0


def test_empty_run_never_passes_gate():
    assert evaluation.summarize_results([], min_score=0, min_pass_rate=0)["exit_code"] == 1


def test_expected_amount_comes_from_canonical_catalog():
    products = [SimpleNamespace(skus=[SimpleNamespace(sku_id="P1001-S1", price=SimpleNamespace(amount_in_minor_units=23456, currency="CNY"))])]
    expected = evaluation.resolve_order_expectation({"order_expectation": {"action": "create", "items": [{"sku_id": "P1001-S1", "quantity": 2}], "status": "CONFIRMED"}}, products)
    assert expected["amount_minor"] == 46912
    assert expected["amount_source"] == "catalog_snapshot"


def test_metadata_records_reproducible_flags_and_case_ids():
    settings = SimpleNamespace(llm_model="test", model_route_mode="direct", reranker_enabled=False,
                               semantic_cache_enabled=True, queue_mode="direct")
    metadata = evaluation.build_run_metadata(settings, [{"id": "c1"}], "run-1", datetime.now(timezone.utc))
    assert metadata["git_commit"]
    assert metadata["case_ids"] == ["c1"] and metadata["run_id"] == "run-1"
    assert metadata["model"] == "test" and metadata["routing"] == "direct"
    assert metadata["cache_effective"] is False and metadata["rerank"] is False
    assert metadata["transport"] == "invoke_agent"

async def test_run_case_perfect_judge_cannot_hide_existing_order(monkeypatch):
    from app.application.agents import main_graph

    async def invoke(*args):
        return "Created GBX-000001"

    async def snapshot(*args):
        return {"GBX-000001": "buyer"}

    async def judge(*args):
        return "SCORE: 2 理由：ok"

    async def find(*args):
        return order()

    async def preferences(*args):
        return []

    monkeypatch.setattr(main_graph, "invoke_agent", invoke)
    monkeypatch.setattr(evaluation, "snapshot_order_ids", snapshot)
    monkeypatch.setattr(evaluation, "judge_case", judge)
    container = SimpleNamespace(agent=None, order_repo=SimpleNamespace(find_by_id=find),
                                preference_store=SimpleNamespace(list_by_buyer=preferences))
    case = {"id": "create", "kind": "order", "buyer_id": "buyer", "query": "buy",
            "must_contain": [], "must_not_contain": [], "resolved_order_expectation": expectation()}
    result = await evaluation.run_case(container, None, case, "run")
    assert result["judge_score"] == 2 and result["score"] == 0
    assert result["task_passed"] is False
    assert any("preexisting" in failure for failure in result["business_failures"])
    assert result["latency_seconds"] >= 0


def test_run_metadata_is_in_machine_readable_and_markdown_report():
    # A report cannot silently omit which business gates changed a perfect judge score.
    result = dict(id="order", kind="order", buyer_id="b", query="buy", must_contain=[],
                  must_not_contain=[], reply="", error="", missing=[], forbidden=[], score=0,
                  parse_error=False, judge_raw="SCORE: 2", reason="ok", business_failures=["buyer mismatch"], judge_score=2)
    report = evaluation.render_report(SimpleNamespace(llm_model="m"), [result], {"run_id": "r1"}, {"exit_code": 1})
    assert "buyer mismatch" in report and "r1" in report and "exit_code" in report

async def test_run_eval_writes_gate_and_provenance_without_real_models(tmp_path, monkeypatch):
    from app import bootstrap
    from app.infrastructure import llm
    from app.infrastructure import settings as settings_module

    settings = SimpleNamespace(llm_model="offline", data_dir=tmp_path, model_route_mode="direct",
                               reranker_enabled=False, semantic_cache_enabled=False)

    async def products():
        return []

    async def close():
        pass

    container = SimpleNamespace(product_repo=SimpleNamespace(list_all=products), close=close)

    async def build(*args):
        return container

    async def failed_case(*args):
        return dict(id="c1", kind="general", buyer_id="b", query="q", must_contain=[],
                    must_not_contain=[], reply="bad", error="", missing=[], forbidden=[], score=0,
                    parse_error=False, judge_raw="SCORE: 0", reason="wrong", task_passed=False,
                    latency_seconds=0.1, business_failures=[])

    monkeypatch.setattr(settings_module, "load_settings", lambda: settings)
    monkeypatch.setattr(llm, "build_chat_model", lambda *_: None)
    monkeypatch.setattr(bootstrap, "build_container", build)
    monkeypatch.setattr(evaluation, "run_case", failed_case)
    case_path = tmp_path / "cases.yaml"
    case_path.write_text("cases:\n- id: c1\n  kind: general\n  query: q\n  buyer_id: b\n  must_contain: []\n  must_not_contain: []\n")
    target = tmp_path / "report.md"
    assert await evaluation.run_eval(case_path, report_path=target, min_score=1, min_pass_rate=1, comparison_id="CURRENT-RUN") == 1
    import json
    artifact = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
    assert artifact["summary"]["exit_code"] == 1
    assert artifact["metadata"]["case_ids"] == ["c1"]
    assert artifact["metadata"]["comparison_id"] == "CURRENT-RUN"
    assert artifact["metadata"]["git_commit"]
    assert artifact["metadata"]["cost"]["kind"] == "unavailable"
    assert artifact["results"][0]["score"] == 0

async def test_missing_query_catches_unreported_order_created_for_other_buyer(monkeypatch):
    from app.application.agents import main_graph

    snapshots = iter([{}, {"GBX-000001": "other-buyer"}])

    async def snapshot(*args):
        return next(snapshots)

    async def invoke(*args):
        return "Requested order does not exist"

    async def judge(*args):
        return "SCORE: 2 理由：ok"

    async def find(order_id):
        return None if order_id == "GBX-999999" else order(buyer_id="other-buyer")

    async def preferences(*args):
        return []

    monkeypatch.setattr(main_graph, "invoke_agent", invoke)
    monkeypatch.setattr(evaluation, "snapshot_order_ids", snapshot)
    monkeypatch.setattr(evaluation, "judge_case", judge)
    container = SimpleNamespace(agent=None, order_repo=SimpleNamespace(find_by_id=find),
                                preference_store=SimpleNamespace(list_by_buyer=preferences))
    case = {"id": "missing", "kind": "order", "buyer_id": "buyer", "query": "query",
            "must_contain": [], "must_not_contain": [],
            "order_expectation": {"action": "absent", "order_id": "GBX-999999"}}
    result = await evaluation.run_case(container, None, case, "run")
    assert result["score"] == 0
    assert "unexpected new order during query" in result["business_failures"]


def _fingerprint_fixture(root):
    files = {"app/core.py": "value = 1\n", "app/nested/service.py": "value = 2\n",
             "scripts/evaluate.py": "print('ok')\n", "eval/cases.yaml": "cases: []\n",
             "uv.lock": "version = 1\n", "pyproject.toml": "[project]\n",
             "frontend/package-lock.json": '{"lockfileVersion": 3}\n'}
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.mark.parametrize("relative", ["app/core.py", "app/nested/service.py", "scripts/evaluate.py", "eval/cases.yaml", "uv.lock", "frontend/package-lock.json"])
def test_source_fingerprint_changes_when_source_or_lock_changes(tmp_path, relative):
    root = _fingerprint_fixture(tmp_path)
    before = evaluation.source_fingerprint(root)
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    assert evaluation.source_fingerprint(root) != before


def test_source_fingerprint_is_portable_and_includes_relative_paths(tmp_path):
    import shutil
    first = _fingerprint_fixture(tmp_path / "first-checkout")
    second = tmp_path / "other-checkout"
    shutil.copytree(first, second)
    assert evaluation.source_fingerprint(first) == evaluation.source_fingerprint(second)
    (second / "app/core.py").rename(second / "app/renamed.py")
    assert evaluation.source_fingerprint(first) != evaluation.source_fingerprint(second)


def test_source_fingerprint_ignores_secrets_docs_runtime_and_generated_files(tmp_path):
    root = _fingerprint_fixture(tmp_path)
    before = evaluation.source_fingerprint(root)
    for relative in [".env", "docs/info.md", "data/result.json", "app/__pycache__/core.pyc", "tests/test_sample.py"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SECRET must never appear in metadata", encoding="utf-8")
    assert evaluation.source_fingerprint(root) == before
    assert "SECRET" not in before
    assert before.startswith("sha256:") and len(before) == 71
