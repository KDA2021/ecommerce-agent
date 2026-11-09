"""Authored task coverage must remain frozen and catalog-consistent."""
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_v2_suite_exists_and_has_grouped_splits():
    from app.infrastructure.persistence.seed_products import build_seed_products
    from scripts.eval_shopping import catalog_snapshot
    from scripts.shopping_suite import load_suite
    catalog = catalog_snapshot(build_seed_products())
    cases = load_suite(ROOT / "eval/shopping_tasks_v2.yaml", catalog)
    assert len(cases) == 32
    assert len({c["id"] for c in cases}) == 32
    assert {split: sum(c["split"] == split for c in cases) for split in
            ("regression", "development", "acceptance")} == {
                "regression": 12, "development": 12, "acceptance": 8}
    assert len(load_suite(ROOT / "eval/shopping_tasks_v2.yaml", catalog, split="acceptance")) == 8


def test_oracle_rejects_invented_eligibility(tmp_path):
    import yaml

    from scripts.shopping_suite import load_suite
    case = {"id": "wrong", "category": "test", "turns": [{"query": "test",
            "oracle": {"candidate_ids": ["P1"], "currency": "CNY", "max_price": "10", "country": "US"},
            "expectation": {"mode": "recommend", "eligible_ids": ["P1"]}}]}
    path = tmp_path / "tasks.yaml"
    path.write_text(yaml.safe_dump({"cases": [case]}), encoding="utf-8")
    with pytest.raises(ValueError, match="oracle"):
        load_suite(path, {"P1": {"price": "20", "currency": "CNY", "ships_to": ["US"]}})


def test_profile_measures_input_size_without_token_or_billing_claims():
    from scripts.profile_shopping_judge import profile_checkpoint
    from tests.test_shopping_replay import fixture_data
    checkpoint, catalog = fixture_data()
    for p in catalog.values():
        p["description"] = "detail " * 100
    report = profile_checkpoint(checkpoint, catalog)
    assert report["model_calls"] == 0
    assert report["turns"] == 1
    assert report["full"]["characters"] > 0
    assert report["estimated_tokens"] is None
    assert report["estimated_money"] is None


def test_plan_only_never_requires_live_authorization_or_calls_models(tmp_path):
    from scripts.eval_shopping import main
    output = tmp_path / "plan.json"
    assert main(["--tasks", str(ROOT / "eval/shopping_tasks_v2.yaml"), "--split", "development",
                 "--repeats", "1", "--plan-only", str(output)]) == 0
    import json
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["tasks"] == 12
    assert report["trajectories"] == 24
    assert report["judge_calls_without_repairs"] == report["agent_turns"]
    assert report["judge_calls_with_repairs_upper_bound"] == 2 * report["agent_turns"]
    assert report["model_calls_made"] == 0
