"""Paired, isolated multi-turn shopping evaluation. Real models are opt-in.

The arms share the current graph and tool inventory. Only planner routing changes;
this does not compare two independently designed agent architectures or HTTP paths.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_schedule(task_ids: list[str], repeats: int) -> list[dict]:
    if (
        not task_ids
        or len(task_ids) != len(set(task_ids))
        or type(repeats) is not int
        or repeats < 1
    ):
        raise ValueError("Unique nonempty tasks and a positive repetition count are required")
    return [
        {"task_id": task, "repetition": repeat, "route": route}
        for repeat in range(1, repeats + 1)
        for index, task in enumerate(task_ids)
        for route in (("direct", "llm") if (index + repeat) % 2 else ("llm", "direct"))
    ]


def summarize_runs(runs: list[dict]) -> dict:
    arms = {}
    for route in ("direct", "llm"):
        rows = [row for row in runs if row["route"] == route]
        latencies = sorted(row["latency_seconds"] for row in rows)
        categories = {}
        for category in sorted({row["category"] for row in rows}):
            group = [row for row in rows if row["category"] == category]
            categories[category] = {
                "passed": sum(row["task_passed"] for row in group),
                "total": len(group),
            }
        arms[route] = {
            "tasks": len(rows),
            "passed": sum(row["task_passed"] for row in rows),
            "task_success_rate": sum(row["task_passed"] for row in rows) / len(rows)
            if rows
            else None,
            "latency_mean_seconds": statistics.mean(latencies) if rows else None,
            "latency_p95_seconds": latencies[math.ceil(0.95 * len(rows)) - 1] if rows else None,
            "llm_calls_mean": statistics.mean(row["llm_calls"] for row in rows) if rows else None,
            "error_count": sum(bool(row.get("error")) for row in rows),
            "categories": categories,
            "agent_error_count": sum(bool(row.get("error")) for row in rows),
            "judge_error_count": sum(
                any(
                    str(f).startswith("judge_error:")
                    for f in turn.get("grading", {}).get("failures", [])
                )
                for row in rows
                for turn in row.get("turns", [])
            ),
            "invalid_extraction_count": sum(
                any(
                    str(f).startswith("extraction_error:")
                    for f in turn.get("grading", {}).get("failures", [])
                )
                for row in rows
                for turn in row.get("turns", [])
            ),
        }
    pairs = {
        "paired_count": 0,
        "both_success": 0,
        "both_failed": 0,
        "planner_only_success": 0,
        "direct_only_success": 0,
    }
    paired = {}
    for row in runs:
        paired.setdefault((row["task_id"], row["repetition"]), {})[row["route"]] = row
    for group in paired.values():
        if set(group) != {"direct", "llm"}:
            continue
        pairs["paired_count"] += 1
        direct, planner = group["direct"]["task_passed"], group["llm"]["task_passed"]
        key = (
            "both_success"
            if direct and planner
            else "both_failed"
            if not direct and not planner
            else ("planner_only_success" if planner else "direct_only_success")
        )
        pairs[key] += 1
    return {"arms": arms, "pairs": pairs}


def validate_resume(expected: dict, stored: dict) -> None:
    for key in (
        "source_fingerprint",
        "catalog_sha256",
        "tasks_sha256",
        "model",
        "repeats",
        "effective_config_sha256",
        "knowledge_sha256",
    ):
        if stored.get(key) != expected.get(key):
            raise ValueError(f"Resume metadata changed: {key}")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def catalog_snapshot(products) -> dict:
    snapshot = {
        product.product_id: {
            "product_id": product.product_id,
            "title": product.title,
            "brand": product.brand,
            "price": f"{product.primary_sku().price.to_major_units():.2f}",
            "currency": product.primary_sku().price.currency,
            "ships_to": product.ships_to,
            "description": product.description,
            "highlights": [{"label": h.label, "detail": h.detail} for h in product.highlights],
            "aliases": list({product.title.split("（")[0].strip()}),
            "sku_prices": [
                {
                    "sku_id": sku.sku_id,
                    "spec": sku.spec,
                    "amount": f"{sku.price.to_major_units():.2f}",
                    "currency": sku.price.currency,
                }
                for sku in product.skus
            ],
        }
        for product in products
    }
    # Reviewed catalog model aliases, not arbitrary fuzzy name matching.
    aliases = {
        "P1002": ["TrailOx 20寸", "TrailOx20寸"],
        "P1015": ["TrailOx 24寸", "TrailOx24寸"],
        "P1003": ["Wanderlite 35L", "Wanderlite35L"],
        "P1005": ["VoltTrek 65W", "VoltTrek65W"],
        "P1024": ["VoltTrek 30W", "VoltTrek30W"],
        "P1025": ["VoltTrek 100W", "VoltTrek100W"],
        "P1004": ["AeroHush Pro"],
        "P1022": ["AeroHush Lite"],
        "P1038": ["LumenGo Mini"],
    }
    for product_id, names in aliases.items():
        if product_id in snapshot:
            snapshot[product_id]["aliases"] = sorted(set(snapshot[product_id]["aliases"] + names))
    return snapshot


def effective_configuration(settings) -> dict:
    """Only reproducibility fields; credentials are never serialized."""
    fields = (
        "llm_model",
        "llm_max_retries",
        "llm_max_concurrency",
        "context_trigger_tokens",
        "context_keep_recent",
        "preference_top_k",
        "tool_timeout_seconds",
        "tool_failure_threshold",
        "tool_circuit_reset_seconds",
        "subagent_timeout_seconds",
        "subagent_max_retries",
        "subagent_lease_seconds",
        "trade_chain_max_steps",
    )
    config = {field: getattr(settings, field, None) for field in fields}
    endpoint = str(getattr(settings, "llm_base_url", ""))
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname += f":{parsed.port}"
    config["provider"] = urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    config["endpoint_sha256"] = hashlib.sha256(endpoint.encode()).hexdigest()
    config.update(
        temperature=0.1,
        thinking="disabled",
        semantic_cache=False,
        reranker=False,
        queue_mode="direct",
        web_search=False,
        route_order=["direct", "llm"],
        context_compression="same settings across arms",
        judge_model=config["llm_model"],
    )
    return config


def _checkpoint(path: Path, metadata: dict, schedule: list, runs: list) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "metadata": metadata,
                "schedule": schedule,
                "runs": runs,
                "summary": summarize_runs(runs),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _counter():
    from langchain_core.callbacks import AsyncCallbackHandler

    class Counter(AsyncCallbackHandler):
        def __init__(self):
            self.calls = 0
            self.usage_calls = 0
            self.total_tokens = 0

        async def on_chat_model_start(self, serialized, messages, **kwargs):
            self.calls += 1

        async def on_llm_end(self, response, **kwargs):
            try:
                message = response.generations[0][0].message
                usage = getattr(message, "usage_metadata", None) or {}
                if isinstance(usage.get("total_tokens"), int):
                    self.total_tokens += usage["total_tokens"]
                    self.usage_calls += 1
            except (IndexError, AttributeError, TypeError):
                pass

        def snapshot(self):
            return {
                "calls": self.calls,
                "usage_calls": self.usage_calls,
                "total_tokens": self.total_tokens if self.usage_calls else None,
            }

    return Counter()


def observed_catalog(catalog: dict, events: list) -> dict:
    result = json.loads(_json(catalog))
    for event in events:
        payload = event[1]
        if event[0] != "tool.result" or payload.get("tool") != "product_search":
            continue
        for hit in payload.get("hits", []):
            quote = hit.get("landed_price") or {}
            product_id = hit.get("product_id")
            if product_id in result and "landed_total_major" in quote:
                result[product_id].setdefault("observed_prices", []).append(
                    {
                        "amount": str(quote["landed_total_major"]),
                        "currency": quote.get("currency")
                        or quote.get("target_currency")
                        or hit.get("currency"),
                        "basis": "landed_total",
                    }
                )
    return result


async def run_trajectory(
    container, judge, task: dict, row: dict, catalog: dict, run_id: str, events: list
) -> dict:
    from langchain_core.messages import HumanMessage

    from app.application.agents.runtime import _final_text
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from scripts.shopping_grading import evaluate_turn, judge_prompt, parse_extraction

    key = hashlib.sha256(task["id"].encode()).hexdigest()[:8]
    buyer = f"shop-{run_id[:8]}-{key}-{row['route']}-{row['repetition']}"
    result = {
        **row,
        "category": task["category"],
        "buyer_id": buyer,
        "turns": [],
        "latency_seconds": 0.0,
        "llm_calls": 0,
        "task_passed": False,
        "error": "",
    }
    session_number = 0
    history = []
    for index, turn in enumerate(task["turns"]):
        if turn.get("fresh_session"):
            session_number += 1
        session_id = f"{buyer}-s{session_number}"
        events.clear()
        counter = _counter()
        context_token = ShoppingContext.set(
            ShoppingContextSnapshot(session_id, buyer, "zh-CN", "CNY", uuid.uuid4().hex)
        )
        started = time.perf_counter()
        reply = error = ""
        public_decision = None
        trace_id = ShoppingContext.current_trace_id()
        try:
            state = await asyncio.wait_for(
                container.agent.ainvoke(
                    {"messages": [HumanMessage(content=turn["query"])]},
                    config={"configurable": {"thread_id": session_id}, "callbacks": [counter]},
                ),
                timeout=120,
            )
            reply = container.data_policy.sanitize_output(_final_text(state.get("messages", [])))
            public_decision = state.get("decision")
        except Exception as exc:
            error = container.data_policy.sanitize_output(f"{type(exc).__name__}: {exc}")
        finally:
            elapsed = time.perf_counter() - started
            ShoppingContext.reset(context_token)
        prefs = [asdict(pref) for pref in await container.preference_store.list_by_buyer(buyer)]
        evidence_catalog = observed_catalog(catalog, events)
        grading = {"passed": False, "failures": [error or "No model reply"], "recommended_ids": []}
        extraction = None
        raw_attempts = []
        judge_counter = _counter()
        if reply and not error:
            prompt = judge_prompt(
                {
                    "id": task["id"],
                    "query": turn["query"],
                    "history": history,
                    "expectation": turn["expectation"],
                },
                reply,
                evidence_catalog,
            )
            # Repair malformed JSON at most once; never retry a valid failing grade.
            for attempt in range(2):
                try:
                    response = await asyncio.wait_for(
                        judge.ainvoke(prompt, config={"callbacks": [judge_counter]}), timeout=90
                    )
                    raw = str(response.content)
                    raw_attempts.append(raw)
                    extraction = parse_extraction(raw)
                    grading = evaluate_turn(
                        turn["expectation"], extraction, reply, evidence_catalog, prefs
                    )
                    break
                except (ValueError, TypeError) as exc:
                    grading = {
                        "passed": False,
                        "failures": [f"extraction_error: {exc}"],
                        "recommended_ids": [],
                    }
                    if attempt == 0:
                        prompt = [
                            *prompt,
                            {
                                "role": "user",
                                "content": "Return only the requested JSON object, no markdown or additional fields.",
                            },
                        ]
                except Exception as exc:
                    grading = {
                        "passed": False,
                        "failures": [f"judge_error: {type(exc).__name__}"],
                        "recommended_ids": [],
                    }
                    break
        usage = counter.snapshot()
        result["latency_seconds"] += elapsed
        result["llm_calls"] += usage["calls"]
        result["turns"].append(
            {
                "turn": index + 1,
                "query": turn["query"],
                "session_id": session_id,
                "trace_id": trace_id,
                "planner_decision": public_decision,
                "trace_file": str(container.settings.data_dir / "traces" / f"{session_id}.jsonl"),
                "reply": reply,
                "error": error,
                "latency_seconds": elapsed,
                "model_usage": usage,
                "judge_usage": judge_counter.snapshot(),
                "expectation": turn["expectation"],
                "extraction": extraction,
                "grading": grading,
                "judge_raw_attempts": raw_attempts,
                "preferences": prefs,
                "tool_events": list(events),
            }
        )
        history.extend(
            [{"role": "user", "content": turn["query"]}, {"role": "assistant", "content": reply}]
        )
        if error:
            result["error"] = error
            break
    result["task_passed"] = len(result["turns"]) == len(task["turns"]) and all(
        t["grading"]["passed"] for t in result["turns"]
    )
    return result


async def run(args) -> int:
    import yaml
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.bootstrap import build_container
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    from app.infrastructure.llm import build_chat_model
    from app.infrastructure.persistence.seed_products import build_seed_products
    from app.infrastructure.settings import load_settings
    from scripts.eval_regression import source_fingerprint

    tasks = yaml.safe_load(args.tasks.read_text(encoding="utf-8"))["cases"]
    if args.case_ids:
        tasks = [task for task in tasks if task["id"] in args.case_ids]
        if {task["id"] for task in tasks} != set(args.case_ids):
            raise ValueError("Unknown or repeated task IDs")
    schedule = build_schedule([task["id"] for task in tasks], args.repeats)
    catalog = catalog_snapshot(build_seed_products())
    settings = load_settings()
    effective_config = effective_configuration(settings)
    knowledge = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "knowledge").rglob("*.md"))
    }
    expected = {
        "source_fingerprint": source_fingerprint(),
        "catalog_sha256": _hash(catalog),
        "tasks_sha256": _hash(tasks),
        "model": settings.llm_model,
        "repeats": args.repeats,
        "effective_config_sha256": _hash(effective_config),
        "knowledge_sha256": _hash(knowledge),
    }
    runs = []
    if args.resume:
        output = args.resume.resolve()
        stored = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
        validate_resume(expected, stored["metadata"])
        if stored["schedule"] != schedule:
            raise ValueError("Resume schedule differs")
        metadata, runs = stored["metadata"], stored["runs"]
        run_id = metadata["run_id"]
    else:
        run_id = uuid.uuid4().hex
        output = args.output.resolve() / run_id
        output.mkdir(parents=True, exist_ok=False)
        metadata = {
            **expected,
            "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "effective_config": effective_config,
            "knowledge_snapshot": knowledge,
            "scope": "authored shopping needs on seeded catalog, not production user traffic",
            "transport": "CompiledGraph.ainvoke with callbacks; same final extraction/data policy as invoke_agent",
            "routing": ["direct", "llm"],
            "temperature": 0.1,
            "semantic_cache": False,
            "reranker": False,
            "queue_mode": "direct",
            "price_basis": "primary SKU sticker price, excluding shipping/tax",
            "latency_policy": "sum of per-turn Agent execution, excludes judging/setup/warmup",
            "call_policy": "LangChain chat model starts; may not include SDK-internal retry requests",
            "usage_policy": "provider-reported message usage only; monetary cost unavailable",
            "design": "serial AB/BA alternating order, two independent buyer namespaces per pair",
        }
    (output / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _checkpoint(output / "checkpoint.json", metadata, schedule, runs)
    print(f"Shopping artifacts: {output}", flush=True)
    admin_url = os.getenv("TEST_MYSQL_ADMIN_URL")
    if not admin_url:
        raise ValueError("TEST_MYSQL_ADMIN_URL must select a disposable test server")
    admin = create_async_engine(admin_url)
    containers, databases, events_by_route = {}, [], {}
    try:
        for route in ("direct", "llm"):
            database = f"shopping_eval_{run_id[:16]}_{route}_{uuid.uuid4().hex[:8]}"
            assert re.fullmatch(r"shopping_eval_[a-z0-9_]+", database)
            async with admin.begin() as conn:
                await conn.execute(text(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4"))
            databases.append(database)
            url = make_url(admin_url).set(database=database).render_as_string(hide_password=False)
            data = output / route / "data"
            arm_settings = replace(
                settings,
                database_url=url,
                data_dir=data,
                chroma_dir=data / "chroma",
                redis_url="",
                tavily_api_key="",
                queue_mode="direct",
                reranker_enabled=False,
                semantic_cache_enabled=False,
                model_route_mode=route,
                buyer_api_keys_json="",
                api_key="",
            )
            container = await build_container(arm_settings)
            containers[route] = container
            actual = catalog_snapshot(await container.product_repo.list_all())
            if _hash(actual) != expected["catalog_sha256"]:
                raise ValueError("Persisted catalog differs from fixed snapshot")
            await container.search_usecase.execute(
                ProductSearchSpec(normalized_query="旅行", top_k=1)
            )
            captured = []
            events_by_route[route] = captured

            def capture(session, kind, payload, original=container.broker.publish, sink=captured):
                if kind.startswith("tool."):
                    sink.append((kind, payload))
                original(session, kind, payload)

            container.broker.publish = capture
        judge = build_chat_model(settings)
        completed = {(row["task_id"], row["repetition"], row["route"]) for row in runs}
        by_id = {task["id"]: task for task in tasks}
        for row in schedule:
            key = (row["task_id"], row["repetition"], row["route"])
            if key in completed:
                continue
            result = await run_trajectory(
                containers[row["route"]],
                judge,
                by_id[row["task_id"]],
                row,
                catalog,
                run_id,
                events_by_route[row["route"]],
            )
            runs.append(result)
            _checkpoint(output / "checkpoint.json", metadata, schedule, runs)
            print(
                json.dumps(
                    {
                        "done": len(runs),
                        "total": len(schedule),
                        **row,
                        "passed": result["task_passed"],
                        "seconds": round(result["latency_seconds"], 2),
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )
        if source_fingerprint() != expected["source_fingerprint"]:
            raise ValueError(
                "Source changed during benchmark; results cannot be treated as one frozen version"
            )
        metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        metadata["complete"] = True
    finally:
        for container in containers.values():
            await container.close()
        async with admin.begin() as conn:
            for database in databases:
                await conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        await admin.dispose()
        metadata["test_databases_deleted"] = True
        _checkpoint(output / "checkpoint.json", metadata, schedule, runs)
    return 0 if metadata.get("complete") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-live", action="store_true", help="Explicitly enable real model calls"
    )
    parser.add_argument("--tasks", type=Path, default=ROOT / "eval/shopping_tasks.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "data/shopping-evaluations")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--case-ids", nargs="+")
    args = parser.parse_args(argv)
    if not args.run_live:
        parser.error("Use --run-live to authorize real model evaluation")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
