import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def comparison_module():
    assert importlib.util.find_spec("scripts.compare_eval"), "comparison entry point missing"
    return importlib.import_module("scripts.compare_eval")


def test_generated_comparison_arguments_are_accepted_by_real_eval_parser(tmp_path, monkeypatch):
    from scripts import eval_regression

    parsed = []

    async def evaluate(**kwargs):
        parsed.append(kwargs)
        return 1

    monkeypatch.setattr(eval_regression, "run_eval", evaluate)

    def runner(command, **_kwargs):
        return SimpleNamespace(returncode=eval_regression.main(command[2:]), stdout="", stderr="")

    comparison_module().run_comparison(tmp_path, Path("eval/cases.yaml"), ["c1"], runner=runner)
    assert len(parsed) == 2
    assert all(item["buyer_suffix"].startswith("-") for item in parsed)


def test_comparison_runs_both_routes_and_keeps_complete_artifacts(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        route = kwargs["env"]["MODEL_ROUTE_MODE"]
        target = Path(command[command.index("--report") + 1]).with_suffix(".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"metadata": {"routing": route, "case_ids": ["c1"], "comparison_id": command[command.index("--comparison-id") + 1], "cost": {"kind": "unavailable"}},
                                     "results": [{"id": "c1", "task_passed": route == "direct", "latency_seconds": 1.5, "error": ""}],
                                     "summary": {"exit_code": 0 if route == "direct" else 1}}))
        return SimpleNamespace(returncode=0 if route == "direct" else 1, stdout="log", stderr="")

    compare_eval = comparison_module()
    code = compare_eval.run_comparison(tmp_path, Path("eval/cases.yaml"), ["c1"], runner=runner)
    assert code == 1  # One failed arm must fail the controlled comparison.
    assert len(calls) == 2
    run_dir = next(tmp_path.glob("*/comparison.json")).parent
    report = json.loads((run_dir / "comparison.json").read_text())
    assert set(report["arms"]) == {"direct", "llm"}
    assert report["arms"]["direct"]["task_success_rate"] == 1
    assert report["arms"]["llm"]["task_success_rate"] == 0
    assert report["arms"]["direct"]["latency_mean_seconds"] == 1.5
    assert report["arms"]["direct"]["cost"]["kind"] == "unavailable"
    assert (run_dir / "direct" / "eval-report.json").is_file()
    assert (run_dir / "llm" / "eval-report.json").is_file()


def test_comparison_attempts_second_arm_after_first_execution_error(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        raise OSError("cannot start")

    compare_eval = comparison_module()
    assert compare_eval.run_comparison(tmp_path, Path("eval/cases.yaml"), ["c1"], runner=runner) == 1
    assert len(calls) == 2
    run_dir = next(tmp_path.glob("*/comparison.json")).parent
    report = json.loads((run_dir / "comparison.json").read_text())
    assert all(arm["execution_error"] for arm in report["arms"].values())
    for route in ("direct", "llm"):
        artifact = json.loads((run_dir / route / "arm-result.json").read_text())
        assert artifact["execution_error"]
        assert artifact["task_success_rate"] is None  # Not-run cases are not fabricated failures.



@pytest.mark.parametrize("xml,code", [
    ('<testsuites><testsuite><testcase name="db"/></testsuite></testsuites>', 0),
    ('<testsuites><testsuite><testcase name="db"><skipped/></testcase></testsuite></testsuites>', 1),
    ('<testsuites/>', 1),
    ('<testsuites><testsuite><testcase name="db"><failure/></testcase></testsuite></testsuites>', 1),
])
def test_integration_gate_rejects_empty_skipped_or_failed_suite(tmp_path, xml, code):
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    assert importlib.util.find_spec("scripts.check_integration_results"), "integration gate missing"
    gate = importlib.import_module("scripts.check_integration_results")
    assert gate.check_results(path) == code


def test_reused_output_cannot_relabel_old_success_as_current_failed_run(tmp_path):
    old_paths = {}
    for route in ("direct", "llm"):
        old = tmp_path / route / "eval-report.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({
            "metadata": {"routing": route, "case_ids": ["c1"], "comparison_id": "OLD-RUN"},
            "results": [{"id": "c1", "task_passed": True, "latency_seconds": 0.01, "error": ""}],
            "summary": {"exit_code": 0},
        }))
        old_paths[old] = old.read_bytes()

    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="new process failed", stderr="failed before reporting")

    compare_eval = comparison_module()
    assert compare_eval.run_comparison(tmp_path, Path("eval/cases.yaml"), ["c1"], runner=runner) == 1
    new_reports = list(tmp_path.glob("*/comparison.json"))
    assert len(new_reports) == 1, "every invocation must write to a fresh comparison directory"
    report = json.loads(new_reports[0].read_text())
    for arm in report["arms"].values():
        assert arm["execution_error"]
        assert arm["task_success_rate"] is None
        assert arm["latency_mean_seconds"] is None
    assert all(path.read_bytes() == original for path, original in old_paths.items())


def test_report_with_wrong_comparison_nonce_is_rejected(tmp_path):
    def runner(command, **kwargs):
        route = kwargs["env"]["MODEL_ROUTE_MODE"]
        target = Path(command[command.index("--report") + 1]).with_suffix(".json")
        target.write_text(json.dumps({
            "metadata": {"routing": route, "case_ids": ["c1"], "comparison_id": "OLD-RUN"},
            "results": [{"id": "c1", "task_passed": True, "latency_seconds": 0.01, "error": ""}],
            "summary": {"exit_code": 0},
        }))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    compare_eval = comparison_module()
    assert compare_eval.run_comparison(tmp_path, Path("eval/cases.yaml"), ["c1"], runner=runner) == 1
    report_path = next(tmp_path.glob("*/comparison.json"))
    report = json.loads(report_path.read_text())
    for arm in report["arms"].values():
        assert "nonce mismatch" in arm["execution_error"]
        assert arm["task_success_rate"] is None
        assert arm["latency_mean_seconds"] is None
