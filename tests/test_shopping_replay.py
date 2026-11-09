"""Offline replay must preserve uncertainty, provenance and independent labels."""
import copy
import hashlib
import json

import pytest


def fixture_data():
    catalog = {"P1": {"title": "Alpha charger", "brand": "Alpha", "price": "89",
                      "currency": "CNY"}}
    extraction = {"recommendations": [{"product_id": "P1", "evidence": "Alpha charger 89元"}],
                  "claims": [], **{k: {"value": False, "evidence": ""}
                                    for k in ("no_match", "clarification", "acknowledgement")}}
    turn = {"turn": 1, "query": "推荐充电器", "reply": "Alpha charger 89元", "error": "",
            "expectation": {"mode": "recommend", "eligible_ids": ["P1"]},
            "extraction": extraction, "grading": {"passed": True, "failures": []},
            "preferences": [], "tool_events": []}
    run = {"task_id": "task-a", "route": "direct", "repetition": 1,
           "task_passed": True, "error": "", "turns": [turn]}
    return {"metadata": {"run_id": "fixture"}, "runs": [run]}, catalog


def replay(checkpoint, catalog, **kwargs):
    from scripts.shopping_replay import replay_checkpoint
    return replay_checkpoint(checkpoint, catalog, **kwargs)


def test_feature_exists():
    from pathlib import Path
    assert (Path(__file__).parents[1] / "scripts/shopping_replay.py").is_file()


def test_replay_does_not_mutate_source_and_preserves_original_verdict():
    checkpoint, catalog = fixture_data()
    checkpoint["runs"][0]["turns"][0]["grading"]["passed"] = False
    original = copy.deepcopy(checkpoint)
    report = replay(checkpoint, catalog)
    row = report["turns"][0]
    assert row["outcome"] == "business_success"
    assert row["original_grading"]["passed"] is False
    assert checkpoint == original
    assert report["summary"]["total_trajectories"] == 1
    assert report["metadata"]["model_calls"] == 0


def test_replay_checks_search_arguments_against_the_frozen_turn():
    from scripts.shopping_replay import digest

    checkpoint, catalog = fixture_data()
    turn = checkpoint["runs"][0]["turns"][0]
    turn["expectation"]["eligible_ids"].append("P2")
    catalog["P2"] = {**catalog["P1"], "title": "Beta charger", "brand": "Beta"}
    tasks = [{"id": "task-a", "turns": [
        {"query": turn["query"], "expectation": turn["expectation"],
         "oracle": {"country": "CN", "currency": "CNY", "max_price": "89"}}
    ]}]
    checkpoint["metadata"]["tasks_sha256"] = digest(tasks)
    turn["tool_events"] = [
        ["tool.invoke", {"tool": "product_search", "args": {
            "normalized_query": "充电器", "ship_to": "US", "target_currency": "USD",
        }}],
        ["tool.invoke", {"tool": "product_search", "args": {
            "normalized_query": "充电器", "ship_to": "CN", "target_currency": "CNY",
            "price_max_major": 89.0,
        }}],
        ["tool.result", {"tool": "product_search", "hits": [{"product_id": "P1"}]}],
    ]
    row = replay(checkpoint, catalog, tasks=tasks)["turns"][0]
    assert row["layers"]["tool_selection"]["status"] == "observed"
    parameters = row["layers"]["tool_parameters"]
    assert parameters["status"] == "mixed"
    assert parameters["calls"] == [
        {"mismatched_fields": ["ship_to", "target_currency", "price_max_major"]},
        {"mismatched_fields": []},
    ]
    assert row["layers"]["retrieval"]["eligible_candidate_coverage"] == 0.5
    assert row["outcome"] == "business_success"
    assert replay(checkpoint, catalog)["turns"][0]["layers"]["tool_parameters"]["status"] == "not_specified"


def test_replay_distinguishes_timeout_from_successful_empty_search():
    checkpoint, catalog = fixture_data()
    turn = checkpoint["runs"][0]["turns"][0]
    turn["tool_events"] = [
        ["tool.invoke", {"tool": "product_search", "args": {}}],
        ["tool.timeout", {"tool": "product_search"}],
        ["tool.result", {"tool": "product_search", "error": "invalid arguments"}],
        ["circuit.open", {"tool": "product_search"}],
        ["tool.result", {"tool": "other_tool", "hits": []}],
    ]
    row = replay(checkpoint, catalog)["turns"][0]
    assert row["layers"]["retrieval"]["status"] == "not_observed"
    assert row["layers"]["retrieval"]["eligible_candidate_coverage"] is None
    execution = row["layers"]["tool_execution"]
    assert execution["status"] == "failure_observed"
    assert execution["failure_events"] == {"tool.timeout": 1, "tool.result_error": 1, "circuit.open": 1}

    turn["tool_events"].append(["tool.result", {"tool": "product_search", "hits": []}])
    row = replay(checkpoint, catalog)["turns"][0]
    assert row["layers"]["tool_execution"]["status"] == "success_and_failure_observed"
    assert row["layers"]["retrieval"]["status"] == "observed"
    assert row["layers"]["retrieval"]["eligible_candidate_coverage"] == 0.0


@pytest.mark.parametrize("failure", ["extraction_error: bad JSON", "judge_error: TimeoutError",
                                   "invalid_input: extraction"])
def test_invalid_extraction_is_inconclusive_not_business_failure(failure):
    checkpoint, catalog = fixture_data()
    turn = checkpoint["runs"][0]["turns"][0]
    turn.update(extraction=None, grading={"passed": False, "failures": [failure]})
    report = replay(checkpoint, catalog)
    assert report["turns"][0]["outcome"] == "inconclusive"
    assert report["summary"]["trajectory_outcomes"]["inconclusive"] == 1
    assert report["summary"]["total_trajectories"] == 1


def test_unverifiable_quote_is_inconclusive_even_when_other_checks_fail():
    checkpoint, catalog = fixture_data()
    checkpoint["runs"][0]["turns"][0]["extraction"]["recommendations"][0]["evidence"] = "invented"
    assert replay(checkpoint, catalog)["turns"][0]["outcome"] == "inconclusive"


def test_execution_error_has_precedence_and_counts_missing_turns():
    checkpoint, catalog = fixture_data()
    checkpoint["runs"][0]["turns"][0]["error"] = "timeout"
    checkpoint["schedule"] = [{"task_id": "task-a", "route": "direct", "repetition": 1},
                              {"task_id": "task-b", "route": "llm", "repetition": 1}]
    report = replay(checkpoint, catalog)
    assert report["turns"][0]["outcome"] == "execution_error"
    assert report["summary"]["total_trajectories"] == 2
    assert report["summary"]["trajectory_outcomes"]["inconclusive"] == 1


def test_missing_preferences_are_inconclusive_but_observed_wrong_state_fails():
    checkpoint, catalog = fixture_data()
    turn = checkpoint["runs"][0]["turns"][0]
    turn["expectation"]["preference"] = {"kind": "dislike", "attribute": "material", "value": "plastic"}
    del turn["preferences"]
    assert replay(checkpoint, catalog)["turns"][0]["outcome"] == "inconclusive"
    turn["preferences"] = []
    row = replay(checkpoint, catalog)["turns"][0]
    assert row["outcome"] == "business_failure"
    assert row["layers"]["business_state"]["status"] == "failed"


def test_splits_group_routes_and_repetitions_by_task():
    checkpoint, catalog = fixture_data()
    second = copy.deepcopy(checkpoint["runs"][0])
    second.update(route="llm", repetition=2)
    checkpoint["runs"].append(second)
    report = replay(checkpoint, catalog)
    assert len({r["split"] for r in report["review_queue"]}) == 1
    assert all(r["human_verdict"] is None for r in report["review_queue"])
    assert report["calibration"]["confirmed_labels"] == 0
    assert report["turns"][0]["layers"]["retrieval"]["status"] == "not_observed"


def test_calibration_requires_human_identity_hash_and_counts_confusion():
    checkpoint, catalog = fixture_data()
    report = replay(checkpoint, catalog)
    label = {**report["review_queue"][0], "human_verdict": "fail", "reviewer": "owner",
             "reviewer_type": "human", "reason": "Wrong recommendation"}
    calibrated = replay(checkpoint, catalog, labels=[label])
    assert calibrated["calibration"]["false_positive"] == 1
    assert calibrated["calibration"]["precision"] == 0
    for change in ({"reviewer_type": "model"}, {"reply_sha256": "wrong"}, {"reviewer": ""}):
        with pytest.raises(ValueError):
            replay(checkpoint, catalog, labels=[{**label, **change}])
    with pytest.raises(ValueError):
        replay(checkpoint, catalog, labels=[label, label])


def test_bound_catalog_hash_is_verified():
    checkpoint, catalog = fixture_data()
    checkpoint["metadata"]["catalog_sha256"] = "mismatch"
    with pytest.raises(ValueError, match="catalog"):
        replay(checkpoint, catalog)


def test_labels_cannot_follow_a_changed_question_or_expectation():
    checkpoint, catalog = fixture_data()
    label = {**replay(checkpoint, catalog)["review_queue"][0], "human_verdict": "pass",
             "reviewer_type": "human", "reviewer": "owner", "reason": "Verified"}
    checkpoint["runs"][0]["turns"][0]["query"] = "Different need"
    with pytest.raises(ValueError, match="context"):
        replay(checkpoint, catalog, labels=[label])


def test_missing_tail_turn_cannot_be_counted_as_success():
    checkpoint, catalog = fixture_data()
    turn = checkpoint["runs"][0]["turns"][0]
    tasks = [{"id": "task-a", "turns": [{"query": turn["query"],
                                         "expectation": turn["expectation"]}, {}]}]
    from scripts.shopping_replay import digest
    checkpoint["metadata"]["tasks_sha256"] = digest(tasks)
    report = replay(checkpoint, catalog, tasks=tasks)
    assert report["trajectories"][0]["outcome"] == "inconclusive"


def test_human_label_remains_valid_when_only_model_extraction_changes():
    checkpoint, catalog = fixture_data()
    label = {**replay(checkpoint, catalog)["review_queue"][0], "human_verdict": "pass",
             "reviewer_type": "human", "reviewer": "owner", "reason": "Verified"}
    checkpoint["runs"][0]["turns"][0]["extraction"]["recommendations"] = []
    assert replay(checkpoint, catalog, labels=[label])["calibration"]["false_negative"] == 1


def test_usage_coverage_never_treats_missing_tokens_as_zero():
    checkpoint, catalog = fixture_data()
    turn = checkpoint["runs"][0]["turns"][0]
    turn["model_usage"] = {"calls": 2, "usage_calls": 1, "total_tokens": 123}
    turn["judge_usage"] = {"calls": 1, "usage_calls": 0, "total_tokens": None}
    usage = replay(checkpoint, catalog)["historical_usage"]
    assert usage["agent"]["reported_tokens"] == 123
    assert usage["agent"]["complete"] is False
    assert usage["judge"]["reported_tokens"] is None


def test_empty_and_duplicate_inputs_fail_closed():
    checkpoint, catalog = fixture_data()
    checkpoint["runs"].append(copy.deepcopy(checkpoint["runs"][0]))
    with pytest.raises(ValueError):
        replay(checkpoint, catalog)
    with pytest.raises(ValueError):
        replay({"metadata": {}, "runs": []}, catalog)


def test_replay_rejects_turns_that_disagree_with_frozen_tasks():
    checkpoint, catalog = fixture_data()
    from scripts.shopping_replay import digest
    turn = checkpoint["runs"][0]["turns"][0]
    tasks = [{"id": "task-a", "turns": [{"query": turn["query"],
                                         "expectation": copy.deepcopy(turn["expectation"])}]}]
    checkpoint["metadata"]["tasks_sha256"] = digest(tasks)
    turn["expectation"]["eligible_ids"] = []
    with pytest.raises(ValueError, match="frozen"):
        replay(checkpoint, catalog, tasks=tasks)


def test_cli_writes_unique_outputs_and_never_overwrites_source(tmp_path):
    from scripts.shopping_replay import main
    checkpoint, catalog = fixture_data()
    source = tmp_path / "checkpoint.json"
    products = tmp_path / "catalog.json"
    source.write_text(json.dumps(checkpoint), encoding="utf-8")
    products.write_text(json.dumps(catalog), encoding="utf-8")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    for _ in range(2):
        assert main(["--checkpoint", str(source), "--catalog", str(products),
                     "--output", str(tmp_path / "outputs")]) == 0
    assert len(list((tmp_path / "outputs").glob("*/report.json"))) == 2
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
