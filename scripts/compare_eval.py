"""Controlled fixed-case direct-route vs planner-route evaluation (real models, opt in).

Uses the invoke_agent harness: HTTP, SSE, queues and cache performance are not measured.
Provider cost is unavailable unless a future harness supplies measured billing/token usage.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_IDS = [
    "search-travel-set-budget",
    "search-luggage-aluminum",
    "search-backpack-foldable",
    "rag-cabin-size-rule",
    "general-math",
]


def run_comparison(
    output_dir: Path,
    cases_path: Path,
    case_ids: list[str],
    *,
    runner=None,
    min_score=0.8,
    min_pass_rate=0.8,
) -> int:
    runner = runner or subprocess.run
    comparison_id = uuid.uuid4().hex
    # A caller may reuse --output; never read or replace evidence from an earlier run.
    output_dir = output_dir / comparison_id
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "comparison_id": comparison_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "case_ids": case_ids,
        "transport": "invoke_agent",
        "arms": {},
        "scope": "direct tool-routing vs planner routing; no HTTP/cache measurement",
        "method": "sequential arms, same fixed ordered cases; unique buyer namespace per arm; default cases read-only",
        "cost_policy": "unavailable; no estimated or measured cost is fabricated",
    }
    exit_code = 0
    for route in ("direct", "llm"):
        arm_dir = output_dir / route
        arm_dir.mkdir(parents=True, exist_ok=True)
        target = arm_dir / "eval-report.md"
        env = dict(os.environ, MODEL_ROUTE_MODE=route, SEMANTIC_CACHE_ENABLED="0")
        command = [
            sys.executable,
            str(ROOT / "scripts" / "eval_regression.py"),
            "--cases",
            str(cases_path.resolve()),
            "--report",
            str(target.resolve()),
            "--min-score",
            str(min_score),
            "--min-pass-rate",
            str(min_pass_rate),
            "--comparison-id",
            comparison_id,
            f"--buyer-suffix=-{comparison_id[:10]}-{route}",
            "--case-ids",
            *case_ids,
        ]
        arm = {
            "routing": route,
            "cost": {"kind": "unavailable"},
            "execution_error": "",
            "task_success_rate": None,
            "latency_mean_seconds": None,
            "latency_p95_seconds": None,
            "error_count": None,
            "artifact": str(target.with_suffix(".json")),
        }
        try:
            process = runner(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            (arm_dir / "stdout.log").write_text(process.stdout or "", encoding="utf-8")
            (arm_dir / "stderr.log").write_text(process.stderr or "", encoding="utf-8")
            arm["process_exit_code"] = process.returncode
            payload = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
            if payload["metadata"].get("comparison_id") != comparison_id:
                raise ValueError("comparison nonce mismatch: report is not from this invocation")
            results = payload["results"]
            if (
                payload["metadata"]["case_ids"] != case_ids
                or [r["id"] for r in results] != case_ids
            ):
                raise ValueError("paired case IDs/order mismatch")
            if payload["metadata"]["routing"] != route:
                raise ValueError("requested/effective routing mismatch")
            latencies = sorted(r["latency_seconds"] for r in results)
            arm.update(
                task_success_rate=sum(r["task_passed"] for r in results) / len(results),
                latency_mean_seconds=statistics.mean(latencies),
                latency_p95_seconds=latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)],
                error_count=sum(bool(r.get("error")) for r in results),
                cost=payload["metadata"].get("cost", {"kind": "unavailable"}),
                metadata=payload["metadata"],
                summary=payload["summary"],
            )
            if process.returncode or payload["summary"]["exit_code"]:
                exit_code = 1
        except (OSError, ValueError, KeyError, ZeroDivisionError) as err:
            arm["execution_error"] = f"{type(err).__name__}: {err}"
            exit_code = 1
        (arm_dir / "arm-result.json").write_text(
            json.dumps(arm, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["arms"][route] = arm
    report["exit_code"] = exit_code
    (output_dir / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Comparison artifacts: {output_dir}")
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "eval-comparisons",
        help="Parent directory; each invocation creates a unique comparison subdirectory",
    )
    parser.add_argument("--cases", type=Path, default=ROOT / "eval" / "cases.yaml")
    parser.add_argument("--case-ids", nargs="+", default=DEFAULT_CASE_IDS)
    parser.add_argument("--min-score", type=float, default=0.8)
    parser.add_argument("--min-pass-rate", type=float, default=0.8)
    args = parser.parse_args(argv)
    # Preserve dataset order so the two arms have identical dependency ordering.
    from scripts.eval_regression import load_cases

    wanted = set(args.case_ids)
    ordered = [case["id"] for case in load_cases(args.cases) if case["id"] in wanted]
    if len(wanted) != len(args.case_ids) or set(ordered) != wanted:
        parser.error("case IDs must be unique and present in the selected dataset")
    return run_comparison(
        args.output, args.cases, ordered, min_score=args.min_score, min_pass_rate=args.min_pass_rate
    )


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
