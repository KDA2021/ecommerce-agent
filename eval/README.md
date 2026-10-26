# Evaluation gate and controlled comparison

The regression evaluator uses the real container and real model when invoked from the command line. Unit tests replace only external execution boundaries; they never send model requests.

```powershell
uv run python scripts/eval_regression.py --min-score 0.8 --min-pass-rate 0.8
uv run python scripts/compare_eval.py
```

`--min-score` is total effective score divided by the maximum score. `--min-pass-rate` is the fraction of cases with effective score 2. Both default to 0.8 and accept values from 0 to 1. An empty selection fails before model calls. The process exits 1 when either threshold is missed. CLI options include `--cases`, `--case-ids`, `--limit`, `--report` and `--with-badcases`.

Order creation requires exactly one newly persisted order, matching the reply's order ID, buyer, SKU, quantity, CONFIRMED status, currency and amount. Expected amount is computed from the catalog snapshot and the case's explicit items, matching the current order use case's sum of item prices. Before/after database ID snapshots distinguish new orders from existing matching orders. Missing-order queries verify actual database absence and reject unexpected creation. A failed business condition caps the effective score at zero even when the language-model judge gives 2; raw judge score and reasons remain in the artifact.

Use a dedicated evaluation database/catalog. Full order evaluation creates orders and consumes inventory. Existing preferences can also influence results; use a fresh synthetic buyer suffix or reset a dedicated fixture between full runs. Run with exclusive database access so concurrent writes cannot affect ID snapshots. This script does not reset or delete application data.

Each run writes Markdown plus a JSON artifact under `data/eval-runs/<run_id>/` by default. `--report path.md` writes a paired `path.json`. Metadata records commit, dirty-worktree flag, a SHA-256 source fingerprint (including uncommitted source and dependency locks, excluding secrets/runtime data), run start, model, judge model, routing, reranking, configured/effective cache, queue setting, transport and ordered case IDs. Per-case artifacts include expected order values, deterministic failures, effective/raw scores, buyer and execution error. Historical reports are preserved by the default unique run directories.

The comparison command runs the existing `MODEL_ROUTE_MODE=direct` and `MODEL_ROUTE_MODE=llm` (planner) paths sequentially against the same five read-only cases. It keeps separate buyer namespaces and disables the semantic cache. This is a direct-routing/planner comparison using `invoke_agent`; it does not measure HTTP authentication, SSE delivery, server cache, or queues. It is not a claim that the direct mode is a separately designed single-agent architecture. Both arms are attempted even when the first fails. Per-arm `arm-result.json` (including execution failures), evaluator JSON/Markdown (when it completes), stdout/stderr logs and `comparison.json` remain under `data/eval-comparisons/<id>/`. The summary records task success rate, Agent-call mean/p95 latency, execution errors and the original evaluation provenance. Provider costs are explicitly `unavailable`; neither measured billing nor estimated prices are invented. A failed/missing arm or mismatched case IDs fails the command. No actual performance result is included until the real command is run with configured dependencies.

A dedicated CI MySQL 8.4 job runs real repository, migration, checkpoint, preference and transaction tests against a disposable service. Test database URLs and root credentials are scoped to that job. Its JUnit gate fails on any skipped test, failure, error, or empty suite. Fast backend and frontend jobs remain available without a database or model key.

`compare_eval.py --output <parent>` always creates a fresh `<parent>/<comparison_id>/` directory, including when the parent already contains old results. Existing artifacts are never deleted or reused. Each subprocess receives `--comparison-id`; the comparison accepts its report only when the returned metadata contains this exact nonce. A process that fails before writing its report produces an execution error with unavailable success/latency, rather than importing earlier results.

## Multi-turn shopping comparison

`shopping_tasks.yaml` freezes 12 authored shopping scenarios (18 turns), including budget/country constraints, comparisons, changed requirements, preference retention/deletion, clarification and honest no-match replies. These are realistic task designs on the seeded catalog, not collected production conversations.

```powershell
# Point this only at a disposable MySQL server with database-create/drop privileges.
$env:TEST_MYSQL_ADMIN_URL='<dedicated test server URL>'
uv run python scripts/eval_shopping.py --run-live --repeats 2
# Resume a checkpoint only with identical source, tasks, catalog and effective configuration:
uv run python scripts/eval_shopping.py --run-live --repeats 2 --resume '<run directory>/checkpoint.json'
```

Two repeats produce 48 trajectories / 72 turns. Each task/repetition uses independent buyer/session namespaces per arm; only a task's explicit fresh-session step reuses that buyer's persisted preferences. Execution is serial with alternating AB/BA order. Both arms disable semantic cache, reranking, external web search and Redis queues. The runner creates and drops its own randomly named databases; it does not reset the application database. Write-order tasks are excluded because the direct main-agent path lacks the planner Trade path's order-write tools.

Artifacts retain full replies, tool events, trace locations, planner decisions, model usage when reported, preference snapshots, raw judge extraction and checked evidence. The judge extracts claims without seeing the expected answer key; programmatic checks apply catalog constraints and exact-quote checks. Unique price lines can inherit an unambiguous nearby product heading, with the expanded original passage and offsets preserved. Semantic extraction still requires auditing; grader failures must be separated from application failures.

Whole-task success requires every turn to pass. Latency sums Agent execution across turns and excludes setup, warmup and judging; model starts may exclude provider SDK-internal retries. The report also retains per-category results and paired outcomes. Small repeated samples describe this frozen evaluation, not production conversion, broad security effectiveness or statistical superiority. Preserve aborted/preflight runs separately and document why they are excluded.
