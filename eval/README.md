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

## Offline replay and grader calibration (zero model calls)

```powershell
.\.venv\Scripts\python.exe scripts/shopping_replay.py --checkpoint '<run directory>/checkpoint.json' --catalog '<run directory>/catalog.json'
```

The command reuses saved extractions and tool-backed price observations, runs the current
programmatic grader, and writes a fresh `data/eval-replays/<id>/` with `report.md`,
`report.json`, and `review.jsonl`. It never calls a model, connects to a database, loads
credentials, overwrites source runs, or imports the production application. Exit zero
means replay completed, not that the Agent passed a quality gate.

The original grades are retained. `business_success` and `business_failure` are provisional
automatic verdicts; `execution_error` identifies Agent failures; `inconclusive` identifies
missing/invalid extraction or unverifiable evidence. All scheduled trajectories remain in
the denominator, including unexecuted ones. A sibling `tasks.json` is automatically checked
against its frozen hash and recorded queries/expectations; `--tasks` can specify it explicitly.
Without frozen tasks, per-trajectory turn completeness cannot be certified.

Diagnostics expose retrieved IDs, eligible candidates returned, and observed preference
state. They do not infer understanding quality or a causal retrieval fault from a failed
answer. Replaying a valid extraction cannot discover semantic omissions or all wrongly
extracted recommendations. Reclassifying a result as inconclusive is not a capability gain.

Review rows start with no human verdict. Reviewers inspect the full reply, expectation,
tool events, preference snapshot and surrounding conversation in `report.json`/the source
checkpoint. Keep only reviewed rows in a separate JSONL; set `human_verdict` to `pass` or
`fail`, `reviewer_type` to `human`, and fill `reviewer` and `reason`. Preserve `case_id`,
`reply_sha256` and `context_sha256`. Then pass `--labels '<reviewed rows>.jsonl'`.
Pending rows and model-generated reviews must not be presented as confirmed human labels.
The CLI validates declared provenance but cannot authenticate reviewer identity.

Calibration reports pass-class precision/recall, agreement, confusion counts and coverage,
with unknown/exception outcomes separated. Missing labels yield unavailable metrics.
Duplicate, stale, unknown or model-attributed labels are rejected. Labels bind query,
reply, conversation history, catalog, expectations and observed business evidence; changing
only the model extraction allows comparing grader versions against the same labels.

Development/replay-holdout membership groups every route and repetition of a task together.
These historical tasks have already been inspected: this split is **not an untouched
acceptance set**. New independent tasks are still needed for final acceptance. Historical
provider-reported Agent/judge tokens are summarized separately with counter coverage;
missing usage and monetary prices remain unavailable. Replay does not rerun a semantic
judge, validate a new prompt, estimate billing, or measure the current Agent's capability.

## Judge cost controls and v2 task suite

Exact-request extraction caching is enabled in the shopping runner. The complete prompt
and non-secret effective model configuration form its key. Only schema-valid raw
extractions are cached; final grades, catalog eligibility and persisted preferences are
always checked again. Invalid output and provider errors are not cached; corrupt entries
are misses. Cache-write errors are recorded and do not discard a valid model response.
Each turn records hits/misses/corruption/write errors, and summaries expose hits/misses.
Cache hits skip model callbacks and actual judge calls. This cache does not accelerate
the Agent or guarantee hits when replies change. Keep the local cache private.

The default `--judge-catalog full` retains full context. Experimental
`--judge-catalog compact` keeps all product identities, aliases, primary/variant prices
and observed quotes, with complete descriptive facts for products mentioned in the query,
history or reply. Shared-brand mentions retain the whole family. Missing descriptive
fields are explicitly unknown. Both modes use only query/history task fields, never
expectations, descriptions, or oracle annotations. Compact mode requires semantic
calibration before it can replace full context in reference results.

```powershell
# Zero-call size profile: this measures characters/bytes, not tokens or money.
.\.venv\Scripts\python.exe scripts/profile_shopping_judge.py --checkpoint '<run>/checkpoint.json' --catalog '<run>/catalog.json'
# Zero-call execution plan; use a new filename on each invocation.
.\.venv\Scripts\python.exe scripts/eval_shopping.py --tasks eval/shopping_tasks_v2.yaml --split development --repeats 1 --plan-only data/eval-plans/dev-plan.json
# Live development run: requires a disposable TEST_MYSQL_ADMIN_URL and model configuration.
.\.venv\Scripts\python.exe scripts/eval_shopping.py --run-live --tasks eval/shopping_tasks_v2.yaml --split development --repeats 1
# Independent judge samples: disable extraction caching explicitly.
# Add --no-judge-cache when measuring judge repeatability/calibrating a semantic judge.
```

`shopping_tasks_v2.yaml` contains 32 authored scenarios: 12 legacy regression cases,
12 new development cases and 8 new acceptance cases (43 authored conversation turns in
total). These are synthetic cases on the seed catalog, not 32 production users or fully
independent task families. Acceptance cases have not been live-run or used to tune the
Agent; they are not a third-party blind test or human-accepted labels. Use development
cases for iteration; retain acceptance cases for a final frozen comparison.

The manifest binds the complete suite and catalog hashes. New oracle annotations check
prices, currencies and shipping against curated candidate IDs; semantic category/material
membership is authored and still needs review. All oracle checks run before API/DB work.
The original 12-case YAML remains the default and is unchanged. `--split` and judge modes
are recorded in effective configuration, so incompatible checkpoints cannot resume.

`--plan-only` makes no model/database calls and reports planned conversation turns and
judge attempts, including at most one malformed-JSON repair per turn. It is not a spending
cap: Agent loops and provider-internal retries have additional calls. Do not infer a Token
or monetary budget from these counts or from the character-reduction profile.

## 2026-11-03 live development run

The v2 development split was run once per route: 24 trajectories / 28 turns using
`deepseek-v4-flash`, full-context judging and the same frozen source. An existing Linux
application image was used after local Chroma initialization failed; the six core runtime
package versions matched the Windows environment. Source and model-cache mounts were
read-only. Temporary MySQL, runner containers and their private network were removed.

The live run reported 83 Agent calls / 230,033 tokens and 31 judge calls / 328,494 tokens.
Four paired compact-judge samples added 4 calls / 30,595 tokens. Total: 118 calls /
589,122 provider-reported tokens; billing amounts were not obtained. The four compact
samples matched extracted facts and verdicts, with four subsequent exact-request cache
hits verified without new calls. Their 29.3% total-token reduction is sample-specific,
not a validated reduction across the whole suite. Full mode remains the default.

Live results exposed landed-price false negatives for reference labels, delivery
qualifiers, equations prefixed by 商品, and short cost-component lists. After adding
positive and negative controls, the rule was fixed and saved responses regraded offline.
Original automatic task passes were Direct 3/12 and Planner 4/12; corrected provisional
passes are 6/12 and 10/12, with respectively 4 and 1 inconclusive trajectories retained.
These are grading changes, not Agent improvements or human-accepted success rates.
204 related checks passed; offline historical replay lost no original passing turns.

Artifacts: `data/eval-live-container-5a998083c18c/REPORT.md`, `summary.json`,
`development/c6cc6d3fbf354c9e9b6cf59fe3ef7f2b/checkpoint.json`, replay before/after,
compact comparisons, source snapshot and `review-needed.md`. Artifacts are local/ignored.
Confirmed human labels remain zero, and the eight acceptance cases have not been run.

## Targeted repair evidence (2026-11-04)

The subsequent repair added bounded keyword supplementation when nonempty vector results
miss named identities, and exposed existing catalog descriptions (400-character cap).
It also repaired duplicate price extraction, multi-span comparisons, table associations,
unique full model aliases and landed-price formatting. Final full tests: 956 passed,
42 skipped; no originally passing historical turns were lost in saved-answer replay.

The old `shopping-v2-card-reader` dual-port requirement is unsupported by the catalog.
`case_quality_annotations.json` records this ambiguity; exclude it from strict dual-port
completion and uplift claims. The annotation is advisory and does not automatically
change runner scores. The original frozen suite and manifest remain unchanged.
`repair_regression.yaml` is a separate, already-inspected development case using the exact
catalog name, price and destination. Both routes passed once live; this is a targeted
confirmation, not independent acceptance. Same-query old/new retrieval probes recovered
P1030 on the historical query with zero additional model calls.

See `data/eval-repair-20261103/REPORT.md` and `summary.json` for source archives, complete
checkpoints, separate replay/rejudge results and remaining semantic-review items. Repair
usage was 28 model starts / 144,561 reported tokens. Human labels remain zero and the
eight acceptance cases remain untouched.

## First acceptance run (2026-11-04)

The eight previously held-out authored tasks have now been run once per route, with
source frozen: 16 trajectories / 22 turns, no execution or extraction-format errors.
Raw automatic passes were Direct 5/8 and Planner 3/8. Failure analysis identified
converted rejected-item prices missing from grading evidence, shortened product names,
landed-price formatting, and incomplete candidate coverage after a budget change.
Actual preference deletion persisted correctly in both routes; the Planner trajectory
failed on a later price-format check. No scores were changed based on this analysis.

Usage: 66 Agent calls / 147,762 tokens and 22 judge calls / 236,346 tokens, totalling
88 calls / 384,108 reported tokens. Human-confirmed labels remain zero. These acceptance
tasks are now inspected data; author a new frozen set if tuning on their failures.
Full evidence and limitations: `data/eval-acceptance-20261104-34b64d035ebb/REPORT.md`.

## Price-contract and retrieval repair (2026-11-04)

Accepted/rejected candidates now retain original sticker prices and separately typed
conversion references and landed totals. Retained events include rejected-price facts.
Grading validates explicit conversion provenance, table-cell ownership and header basis,
and unique contextual comparison evidence. Generic category recall supplements partial
vector matches; a reviewed small alias vocabulary handles camping lights and cookware.
Shared answer instructions reduce unrelated product lists, and frontend comparisons use
the actual landed currency.

Final backend checks: 1037 passed / 42 skipped; frontend: 34 Vitest + 69 Node checks and
build passed. Three known development scenarios were run once (6 trajectories / 8 turns);
raw original verdicts were retained. After structural grading repairs, saved replies
replayed at 7/8 turns, with the actual Planner cookware miss still failing. A subsequent
targeted cookware run passed both routes after the compound-category fix. These are
development validations, not a general success-rate estimate or human acceptance.

`shopping_contract_regression.yaml` contains the three inspected development cases.
`shopping_contract_acceptance.yaml` freezes six new, unrun combinations from the same
catalog/task families. Repair usage: 42 model starts / 184,854 reported tokens. Full
provenance and remaining limits: `data/eval-contract-repair-20261104/REPORT.md`.

## Tool flow and retrieval diagnostics (2026-11-06)

Run `python -m pytest -q tests/test_shopping_tool_flow.py` for the scripted-model
regression on Direct and Planner. It covers clarification without searching,
matching and empty results, missing search terms, invalid `top_k`, timeout
delivery, and a subsequent successful call. The real graphs and guarded
`product_search` run against a controlled search backend. Model decisions and
replies are scripted so the tests measure the wiring and error propagation.

In the offline replay, provide the frozen `tasks.json` to compare recorded
`product_search` arguments with the authored country, currency and price cap.
The report separates invocation evidence, argument mismatches, successful and
failed tool results, eligible candidate coverage, and answer grading. Coverage
uses the union of successful results for the turn. An empty successful result
gives zero coverage when eligible products exist. Without a successful result
or an eligible set, coverage is unavailable. A mix of successful and failed
events is reported separately because saved traces lack a per-call correlation
ID for proving recovery. Schema rejection may occur before the tool publishes
an invocation, so missing event evidence is inconclusive.

On the historical 22-turn checkpoint, 18 turns called search and matched all
three authored argument constraints. The four preference turns had no search
requirement. On the Direct budget-change turn, the search arguments matched
and the answer passed, while the retained results contained only one of two
eligible lamps. Replay used zero model and database calls. The fresh acceptance
suite and human calibration are separate evaluations.
