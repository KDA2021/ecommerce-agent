"""Replay retained shopping evidence with zero model/database calls.

Automatic verdicts are provisional. Reusing an extraction does not validate its
semantic completeness; confirmed human labels are required for calibration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_shopping import observed_catalog  # noqa: E402 -- support direct CLI execution
from scripts.shopping_grading import evaluate_turn, parse_extraction  # noqa: E402

OUTCOMES = ("business_success", "business_failure", "execution_error", "inconclusive")
# These checks indicate unreliable/missing evidence, not proven application faults.
UNCERTAIN = (
    "invalid_input:",
    "extraction_error:",
    "judge_error:",
    "missing_persisted_preference_snapshot",
    "evidence_not_in_reply",
    "evidence_missing_product_anchor",
    "rejected_choice",
    "amount_text_not_in_evidence",
    "amount_text_mismatch",
    "currency_not_grounded",
    "invalid_amount:",
    "evidence_missing_semantic_anchor",
    "missing_two_product_anchors",
    "invalid_expectation_mode",
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def identity(row: dict) -> tuple:
    return row["task_id"], row["route"], row["repetition"]


def outcome(grading: dict, error: str) -> str:
    if error:
        return "execution_error"
    if any(marker in str(f) for f in grading["failures"] for marker in UNCERTAIN):
        return "inconclusive"
    return "business_success" if grading["passed"] else "business_failure"


def search_parameters(invocations: list[dict], oracle: dict | None) -> dict:
    if oracle is None:
        return {"status": "not_specified", "calls": []}
    if not invocations:
        return {"status": "not_observed", "calls": []}
    calls = []
    for args in invocations:
        mismatches = [
            arg
            for arg, key in (("ship_to", "country"), ("target_currency", "currency"))
            if str(args.get(arg, "")).strip().upper() != oracle[key]
        ]
        try:
            price_matches = Decimal(str(args.get("price_max_major"))) == Decimal(
                str(oracle["max_price"])
            )
        except InvalidOperation:
            price_matches = False
        if not price_matches:
            mismatches.append("price_max_major")
        calls.append({"mismatched_fields": mismatches})
    matched = sum(not call["mismatched_fields"] for call in calls)
    status = "matched" if matched == len(calls) else "mixed" if matched else "mismatched"
    return {"status": status, "calls": calls}


def diagnose(turn: dict, grading: dict, oracle: dict | None = None) -> dict:
    failures = grading["failures"]
    state_failures = [f for f in failures if "preference" in f]
    events = turn.get("tool_events", [])
    hits = set()
    invocations = []
    successful_results = 0
    failure_events = Counter()
    for kind, payload in events:
        if payload.get("tool") != "product_search":
            continue
        if kind == "tool.invoke":
            invocations.append(payload.get("args") or {})
        elif kind in ("tool.error", "tool.timeout", "circuit.open"):
            failure_events[kind] += 1
        elif kind == "tool.result":
            if payload.get("error"):
                failure_events["tool.result_error"] += 1
            elif isinstance(payload.get("hits"), list):
                successful_results += 1
                hits.update(h["product_id"] for h in payload["hits"] if h.get("product_id"))
    search_observed = bool(successful_results)
    execution_status = "not_observed"
    if search_observed:
        execution_status = "success_and_failure_observed" if failure_events else "success_observed"
    elif failure_events:
        execution_status = "failure_observed"
    elif invocations:
        execution_status = "result_not_observed"
    eligible = set(turn["expectation"].get("eligible_ids", []))
    relevant = sorted(eligible.intersection(hits))
    needed_state = any(k in turn["expectation"] for k in ("preference", "forbidden_preferences"))
    return {
        "understanding": {
            "status": "not_evaluated",
            "reason": "No semantic understanding judge was run.",
        },
        "tool_selection": {
            "status": "observed"
            if invocations or search_observed or failure_events
            else "not_observed",
            "search_required": turn["expectation"]["mode"] in ("recommend", "no_match"),
            "recorded_invocations": len(invocations),
        },
        "tool_parameters": search_parameters(invocations, oracle),
        "tool_execution": {
            "status": execution_status,
            "successful_results": successful_results,
            "failure_events": dict(failure_events),
        },
        "retrieval": {
            "status": "observed" if search_observed else "not_observed",
            "returned_ids": sorted(hits),
            "eligible_returned_ids": relevant,
            "eligible_candidate_coverage": len(relevant) / len(eligible)
            if eligible and search_observed
            else None,
            "reason": "Candidate visibility only; no causal recall diagnosis.",
        },
        "tool_facts": {
            "status": "available" if turn.get("tool_events") else "not_observed",
            "reason": "Retained payloads are available; fact completeness is not certified.",
        },
        "answer": {
            "status": "provisional_pass" if grading["passed"] else "requires_review",
            "checks": [f for f in failures if f not in state_failures],
        },
        "business_state": {
            "status": "not_required"
            if not needed_state
            else "not_observed"
            if turn.get("preferences") is None
            else "failed"
            if state_failures
            else "passed",
            "checks": state_failures,
        },
    }


def calibration(rows: list[dict], labels: list[dict]) -> dict:
    by_id = {row["case_id"]: row for row in rows}
    counts = Counter()
    splits = {}
    seen = set()
    for label in labels:
        case_id = label["case_id"]
        if case_id in seen or case_id not in by_id:
            raise ValueError("Duplicate or unknown calibration case_id")
        seen.add(case_id)
        row = by_id[case_id]
        if label.get("reply_sha256") != row["reply_sha256"]:
            raise ValueError("Human label reply hash mismatch")
        if label.get("context_sha256") != row["context_sha256"]:
            raise ValueError("Human label context hash mismatch")
        if (
            label.get("reviewer_type") != "human"
            or not str(label.get("reviewer", "")).strip()
            or not str(label.get("reason", "")).strip()
            or label.get("human_verdict") not in ("pass", "fail")
        ):
            raise ValueError(
                "Calibration requires an identified human, reason and pass/fail verdict"
            )
        counts["confirmed_labels"] += 1
        split_counts = splits.setdefault(row["split"], Counter())
        if row["outcome"] not in ("business_success", "business_failure"):
            counts["unscored_labels"] += 1
            split_counts["unscored_labels"] += 1
            continue
        prediction = row["outcome"] == "business_success"
        actual = label["human_verdict"] == "pass"
        key = (
            ("true_positive" if actual else "false_positive")
            if prediction
            else ("false_negative" if actual else "true_negative")
        )
        counts[key] += 1
        split_counts[key] += 1

    def metrics(c):
        tp, fp, fn, tn = (
            c[k] for k in ("true_positive", "false_positive", "false_negative", "true_negative")
        )
        return {
            **{
                k: c[k]
                for k in ("true_positive", "false_positive", "false_negative", "true_negative")
            },
            "scored_labels": tp + fp + fn + tn,
            "unscored_labels": c["unscored_labels"],
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "agreement": (tp + tn) / (tp + fp + fn + tn) if tp + fp + fn + tn else None,
        }

    return {
        **metrics(counts),
        "confirmed_labels": counts["confirmed_labels"],
        "by_split": {s: metrics(c) for s, c in splits.items()},
        "label_policy": "Explicit human declarations only; reviewer identity is not authenticated by this CLI.",
    }


def replay_checkpoint(checkpoint: dict, catalog: dict, *, labels=None, tasks=None) -> dict:
    metadata = checkpoint["metadata"]
    if metadata.get("catalog_sha256") and metadata["catalog_sha256"] != digest(catalog):
        raise ValueError("Frozen catalog hash mismatch")
    if tasks is not None and metadata.get("tasks_sha256") != digest(tasks):
        raise ValueError("Frozen tasks hash mismatch")
    task_lengths = {task["id"]: len(task["turns"]) for task in tasks or []}
    frozen_tasks = {task["id"]: task for task in tasks or []}
    runs = checkpoint["runs"]
    schedule = checkpoint.get("schedule") or runs
    if not schedule:
        raise ValueError("Empty replay cannot establish a baseline")
    expected = [identity(row) for row in schedule]
    if len(expected) != len(set(expected)):
        raise ValueError("Duplicate scheduled trajectory")
    completed = {identity(row): row for row in runs}
    if len(completed) != len(runs) or not set(completed).issubset(expected):
        raise ValueError("Duplicate or unscheduled trajectory")
    if tasks is not None and any(key[0] not in task_lengths for key in expected):
        raise ValueError("Scheduled task missing from frozen tasks")
    rows, trajectories, review_queue = [], [], []
    for key in expected:
        run = completed.get(key)
        base = dict(zip(("task_id", "route", "repetition"), key))
        if run is None:
            trajectories.append(
                {**base, "outcome": "inconclusive", "reason": "Scheduled but not executed"}
            )
            continue
        turn_outcomes = []
        observed_turns = run["turns"]
        if [t["turn"] for t in observed_turns] != list(range(1, len(observed_turns) + 1)):
            raise ValueError("Non-contiguous or duplicate turn numbers")
        for turn in observed_turns:
            oracle = None
            if tasks is not None:
                frozen_turns = frozen_tasks[key[0]]["turns"]
                if turn["turn"] > len(frozen_turns):
                    raise ValueError("Extra turn not in frozen tasks")
                frozen = frozen_turns[turn["turn"] - 1]
                if any(turn.get(k) != frozen.get(k) for k in ("query", "expectation")):
                    raise ValueError("Recorded turn disagrees with frozen task")
                oracle = frozen.get("oracle")
            reply = turn.get("reply", "")
            extraction = turn.get("extraction")
            extraction_source = "saved_extraction"
            if extraction is None:
                extraction_source = "unavailable"
                for raw_index in range(len(turn.get("judge_raw_attempts", [])) - 1, -1, -1):
                    try:
                        extraction = parse_extraction(turn["judge_raw_attempts"][raw_index])
                    except (ValueError, TypeError):
                        continue
                    extraction_source = f"raw_attempt:{raw_index}"
                    break
            if extraction is None:
                grading = {
                    "passed": False,
                    "failures": ["extraction_error: no saved valid extraction"],
                }
            else:
                grading = evaluate_turn(
                    turn["expectation"],
                    extraction,
                    reply,
                    observed_catalog(catalog, turn.get("tool_events", [])),
                    turn.get("preferences"),
                )
            current = outcome(grading, turn.get("error", ""))
            split = "replay_holdout" if int(digest(key[0])[:8], 16) % 4 == 0 else "development"
            case_id = digest([metadata.get("run_id"), *key, turn["turn"]])
            row = {
                **base,
                "turn": turn["turn"],
                "case_id": case_id,
                "split": split,
                "reply_sha256": hashlib.sha256(reply.encode()).hexdigest(),
                "context_sha256": digest(
                    {
                        "catalog": catalog,
                        "turn": {
                            k: turn.get(k)
                            for k in ("query", "reply", "expectation", "preferences", "tool_events")
                        },
                        "prior_turns": [
                            {k: t.get(k) for k in ("query", "reply")}
                            for t in observed_turns[: turn["turn"] - 1]
                        ],
                    }
                ),
                "query": turn["query"],
                "reply": reply,
                "outcome": current,
                "original_grading": turn.get("grading"),
                "extraction_source": extraction_source,
                "grading": grading,
                "layers": diagnose(turn, grading, oracle),
            }
            rows.append(row)
            turn_outcomes.append(current)
            review_queue.append(
                {
                    **row,
                    "expectation": turn["expectation"],
                    "tool_events": turn.get("tool_events", []),
                    "preferences": turn.get("preferences"),
                    "human_verdict": None,
                    "reviewer_type": None,
                    "reviewer": "",
                    "reason": "",
                }
            )
        if run.get("error") or "execution_error" in turn_outcomes:
            current = "execution_error"
        elif (
            not turn_outcomes
            or "inconclusive" in turn_outcomes
            or (key[0] in task_lengths and len(observed_turns) != task_lengths[key[0]])
        ):
            current = "inconclusive"
        elif "business_failure" in turn_outcomes:
            current = "business_failure"
        else:
            current = "business_success"
        trajectories.append(
            {**base, "outcome": current, "original_task_passed": run.get("task_passed")}
        )
    count = Counter(t["outcome"] for t in trajectories)
    by_route = {}
    for route in sorted({t["route"] for t in trajectories}):
        route_rows = [t for t in trajectories if t["route"] == route]
        route_counts = Counter(t["outcome"] for t in route_rows)
        by_route[route] = {
            "total": len(route_rows),
            **{k: route_counts[k] for k in OUTCOMES},
            "provisional_success_rate": route_counts["business_success"] / len(route_rows),
        }
    return {
        "metadata": {
            "schema_version": 1,
            "source_run_id": metadata.get("run_id"),
            "source_checkpoint_sha256": digest(checkpoint),
            "catalog_sha256": digest(catalog),
            "grader_sha256": hashlib.sha256(
                (ROOT / "scripts/shopping_grading.py").read_bytes()
            ).hexdigest(),
            "replay_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "model_calls": 0,
            "database_calls": 0,
            "turn_completeness_verified": tasks is not None,
            "scope": "Programmatic replay of saved extraction; no new semantic judgment or Agent run.",
            "split_policy": "Task-grouped replay split; historical data has already been inspected and is not an untouched acceptance set.",
        },
        "summary": {
            "total_trajectories": len(trajectories),
            "total_turns": len(rows),
            "trajectory_outcomes": {k: count[k] for k in OUTCOMES},
            "search_diagnostics": {
                layer: dict(Counter(row["layers"][layer]["status"] for row in rows))
                for layer in ("tool_selection", "tool_parameters", "tool_execution", "retrieval")
            },
            "by_route": by_route,
        },
        "trajectories": trajectories,
        "turns": rows,
        "review_queue": review_queue,
        "calibration": calibration(rows, labels or []),
        "historical_usage": historical_usage(runs),
    }


def historical_usage(runs: list[dict]) -> dict:
    """Reported usage only; absent counters are never converted to measured zero."""
    result = {"monetary_cost": None, "scope": "Source-run usage; replay itself makes zero calls."}
    for name, field in (("agent", "model_usage"), ("judge", "judge_usage")):
        counters = [turn.get(field) or {} for run in runs for turn in run["turns"]]
        tokens = [c["total_tokens"] for c in counters if c.get("total_tokens") is not None]
        calls = sum(c.get("calls", 0) for c in counters)
        covered_calls = sum(c.get("usage_calls", 0) for c in counters)
        result[name] = {
            "reported_tokens": sum(tokens) if tokens else None,
            "recorded_calls": calls,
            "usage_covered_calls": covered_calls,
            "complete": bool(counters) and len(tokens) == len(counters) and calls == covered_calls,
        }
    return result


def render_report(report: dict) -> str:
    lines = [
        "# Offline shopping replay",
        "",
        report["metadata"]["scope"],
        "",
        "No model or database calls. Automatic results are provisional, not human-verified success.",
        "",
        "| Route | Total | Success | Failure | Execution error | Inconclusive |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for route, row in report["summary"]["by_route"].items():
        lines.append(
            f"| {route} | {row['total']} | " + " | ".join(str(row[k]) for k in OUTCOMES) + " |"
        )
    lines.extend(
        [
            "",
            "## Historical token usage",
            "",
            json.dumps(report["historical_usage"], ensure_ascii=False, indent=2),
            "",
            "## Calibration",
            "",
            json.dumps(report["calibration"], ensure_ascii=False, indent=2),
            "",
            "Historical replay holdout is not an untouched acceptance set.",
            "",
            "## Turn diagnostics",
            "",
        ]
    )
    for row in report["turns"]:
        lines.append(
            f"- {row['task_id']} / {row['route']} / repeat {row['repetition']} / turn {row['turn']}: {row['outcome']}"
        )
        layers = row["layers"]
        lines.append(
            f"  - Product search: {layers['tool_selection']['status']}; "
            f"parameters: {layers['tool_parameters']['status']}; "
            f"execution: {layers['tool_execution']['status']}; "
            f"eligible candidate coverage: {layers['retrieval']['eligible_candidate_coverage']}"
        )
        lines.extend(f"  - {failure}" for failure in row["grading"]["failures"])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument(
        "--tasks", type=Path, help="Frozen tasks; otherwise use sibling tasks.json if present"
    )
    parser.add_argument("--labels", type=Path, help="JSONL of completed human review rows")
    parser.add_argument("--output", type=Path, default=ROOT / "data/eval-replays")
    args = parser.parse_args(argv)
    try:
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
        tasks_path = args.tasks or args.checkpoint.with_name("tasks.json")
        if args.tasks and not tasks_path.is_file():
            raise ValueError("Explicit tasks file does not exist")
        tasks = json.loads(tasks_path.read_text(encoding="utf-8")) if tasks_path.is_file() else None
        labels = (
            [
                json.loads(line)
                for line in args.labels.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if args.labels
            else []
        )
        report = replay_checkpoint(checkpoint, catalog, labels=labels, tasks=tasks)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    report["metadata"]["created_at"] = datetime.now(timezone.utc).isoformat()
    report["metadata"]["source_file_sha256"] = hashlib.sha256(
        args.checkpoint.read_bytes()
    ).hexdigest()
    report["metadata"]["labels_sha256"] = digest(labels)
    output = args.output.resolve() / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(render_report(report), encoding="utf-8")
    (output / "review.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in report["review_queue"]),
        encoding="utf-8",
    )
    print(f"Offline replay: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
