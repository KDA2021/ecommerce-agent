# ecommerce-agent

Globex 学习版：MySQL 8.4 + Chroma + LangGraph 子 Agent 的跨境电商导购 Agent（P9 可选 Redis，P11 工程化加固，P14 可维护性重构，P15 工程标准）。

## 技术栈

- Python 3.11+ + uv
- LangGraph + LangChain（planner / search_agent / trade_agent 子图，Send 并行）
- SQLAlchemy async + asyncmy
- Alembic（MySQL schema 版本管理与迁移，P11-2）
- MySQL 8.4（业务数据 + LangGraph checkpointer + 长期偏好记忆）
- Chroma（本地商品向量召回 + 品类知识 RAG + 语义缓存）
- Redis 7（P9 可选：语义缓存热路径 + 滑动窗口限流 + 幂等快路径；P11-4 可选：Redis Stream 任务队列 + Pub/Sub 事件背板；`REDIS_URL` 为空即整体关闭）
- BGE Reranker（`BAAI/bge-reranker-base` + `sentence-transformers`，本地 CPU 精排）
- FastAPI（REST + SSE，事件推送）
- React 18 + Vite + TypeScript（`frontend/`）
- 品类 RAG + 偏好记忆：Markdown 知识库切块入库，买家 `like`/`dislike` 偏好跨会话注入并参与缓存失效
- P8 可观测与可信执行：JSONL span trace + 延迟分位数报告（`scripts/latency_report.py`）、
  MySQL 幂等下单与任务证据表（`agent_tasks`）、子 Agent deadline/lease/generation fencing
- P11 工程化加固：订单号数据库原子序列、Alembic 迁移、API Key 鉴权 + trace 脱敏、
  可选多 worker（Redis Stream 队列 + Pub/Sub 事件背板）
- P15 工程标准：GitHub Actions CI（pytest + 前端 build，无需真实 LLM key / MySQL）、
  Makefile 统一入口、pre-commit（ruff format + ruff check）、
  配置分组 + JSON 结构化日志、Docker 收口（后端 healthcheck + 前端 nginx 生产镜像）

## 可靠业务闭环优化

- **交易一致性**：下单/取消的订单、明细和库存共用 MySQL 事务；按稳定顺序锁定商品行，失败整体回滚，同幂等键重放校验买家与请求内容。
- **用户归属**：配置 `BUYER_API_KEYS_JSON`（买家编号到独立随机访问凭证的 JSON 映射）启用严格隔离。浏览器输入个人凭证后使用 HttpOnly、SameSite Cookie；订单、会话、SSE、任务、反馈与指标均校验归属，Agent 订单工具也校验当前买家。严格模式不接受共享 `API_KEY` 绕过。
- **多轮任务**：planner 与子任务派发显式携带有界历史、摘要和前序结果，保留商品/SKU/订单引用。
- **交互恢复**：HTTP 错误显示后端原因，队列任务按 `client_request_id`/任务号关联事件；本地等待超时继续保留任务与轮询。下单确认请求失败可重试，收到服务端决议后才更新终态。
- **业务验收**：真实 MySQL 事务回归、React 运行时测试和 CI；评测独立核对订单业务后置条件，未达门槛非零退出，按运行目录和来源指纹保存证据。运行方式见 [评测说明](eval/README.md)。

启用独立用户模式前，执行 `uv run python scripts/migrate.py --auto-stamp` 升级到最新结构，再在本机 `.env` 配置 `BUYER_API_KEYS_JSON`。每个凭证至少 16 个字符且互不重复，建议使用 `secrets.token_urlsafe(32)` 生成。HTTPS 部署设置 `AUTH_COOKIE_SECURE=1`。严格模式会话编号仅允许小写字母、数字、下划线和短横线，避免日志文件名别名；未登记归属的旧会话需要显式迁移，页面可新建会话使用。

`BUYER_API_KEYS_JSON` 留空时保留本地演示/兼容服务密钥模式。网页支持 Cookie 登录，共享服务密钥不会打包到前端。真实 `.env` 与本地 `docs/` 继续排除在 Git 之外。

```powershell
# 后端回归；TEST_MYSQL_URL 仅指向专用测试数据库
uv run pytest -q
# 前端交互测试与生产构建
npm --prefix frontend test
npm --prefix frontend run build
```

## 启动

1. 启动 MySQL 8.4（容器名 `ecommerce-mysql`，端口 `3307:3306`）：

   ```powershell
   docker compose -f docker/docker-compose.p1.yaml up -d
   # 首次创建后，后续启动：
   docker start ecommerce-mysql
   ```

   一行起全栈（MySQL + Redis + app + 前端生产镜像，见 [Docker 部署](#docker-部署)）：

   ```powershell
   docker compose -f docker/docker-compose.yaml up -d --build
   #   → 前端 http://localhost / API http://localhost:8000/docs
   ```

   > **P9 可选 Redis**：`docker/docker-compose.yaml` 已内置 `redis:7-alpine`
   > （容器名 `ecommerce-redis`，端口 `6379`），可用
   > `docker compose -f docker/docker-compose.yaml up -d mysql redis` 单独起。
   > `.env` 里 `REDIS_URL` 留空则不连 Redis，缓存 / 限流 / 幂等自动降级为 P8 行为。

2. 配置环境变量：

   ```powershell
   cp .env.example .env
   # 填入 LLM_API_KEY
   ```

3. 初始化 / 升级数据库 schema（Alembic，P11-2）：

   ```powershell
   # 升到最新版本；空库会建出全部表
   uv run python scripts/migrate.py

   # 已有库是 bootstrap_schema（create_all）建出来的、没有 alembic_version 时：
   # 先 stamp head 记录基线再升级，不会因为"表已存在"报错
   uv run python scripts/migrate.py --auto-stamp
   ```

   > 服务启动时的 `bootstrap_schema` 仍然保留：它用 `create_all` 补齐缺失的表
   > + 补 `orders.idempotency_key` 列，是开发 / 测试的快速路径；生产 / 多环境
   > 变更请用 Alembic 新增 revision，不要再改 `create_all` 能覆盖的范围。

4. 启动 API：

   ```powershell
   uv sync
   uv run uvicorn app.presentation.server:app --port 8000
   ```

5. 启动前端（另开终端）：

   ```powershell
   cd frontend && npm install && npm run dev
   ```

6. 验证：

   ```powershell
   curl http://localhost:8000/health
   ```

## API

> **鉴权（P11-3）**：`API_KEY` 为空时 `/commerce/*` 全部开放（本地开发默认）；
> 配置后所有 `/commerce/*` 都需要 `X-API-Key` 请求头或 `?api_key=` 查询参数，
> 缺失 / 错误返回 401，`/health`、`/docs`、`/openapi.json` 始终开放。

- `POST /commerce/intents`：提交一轮对话意图，返回最终回复；
  `QUEUE_MODE=redis` 时只入队并立即返回 `task_id` + `status=queued`
- `GET /commerce/intents/{task_id}`：查询队列任务状态（`queued / running / done / failed`，仅 redis 模式）
- `GET /commerce/events?shopping_session_id=...`：SSE 事件流（`tool.invoke` / `tool.result` / `agent.dispatch` / `cache.hit` / `context.compressed` / `final.result` / `error`）
- `GET /commerce/orders/{order_id}`：查询订单
- `POST /commerce/orders/{order_id}/cancel`：取消订单

## P2 特性

- **品类知识 RAG**：`knowledge/*.md` 按 `##` 标题切块，本地 embedding 写入 Chroma
  `category_kb`；`category_insight_tool` 回答“怎么挑 / 什么价位 / 有什么坑”，
  命中片段带 `source` / `heading` / `score`，知识库不可用时返回 `{"error": ...}` 不编造。
- **长期偏好记忆**：MySQL `buyer_preferences` 表持久化买家 `like` / `dislike` 偏好；
  主 Agent 通过 `remember_preference_tool` / `forget_preference_tool` 写入与撤回，
  每轮在压缩后自动注入 `<buyer-preferences>` hint（MainAgent 与 SearchAgent，TradeAgent 不注入）。
  安全底线：`dislike` 全量保留、不参与 top_k 截断；`like` 按时间倒序取最近
  `PREFERENCE_TOP_K` 条。
- **缓存作用域指纹**：服务层按 `sha256(渲染后的偏好正文)[:16]` 计算 scope，
  作为语义缓存的分桶键；偏好新增 / 撤回后指纹变化，旧缓存自动不再命中。

## P3 特性：BGE Reranker 二阶段召回

- **召回链路**：`query → Chroma embedding 召回 top 8 → BGE Reranker 精排 → 硬过滤 → top_k`。
- **模型**：`BAAI/bge-reranker-base`，`sentence-transformers` 的 `CrossEncoder` 本地 CPU 推理，
  服务启动时预热并下载模型（`bootstrap.py` 在装配工具/主图前 `await warmup()`），之后走 Hugging Face cache，
  首次请求不再付冷加载成本。
- **失败可降级**：reranker 未启用、加载失败或推理异常时，自动退回向量分排序，
  结果里如实标注 `rerank_applied=false`，`recall_strategy` 落在
  `embedding_rerank → embedding_only → keyword_2gram` 降级链的对应档位。
- **装配**：`bootstrap.py` 按 `RERANKER_ENABLED` 决定是否构造 `BgeReranker`，
  注入 `CatalogSearchUseCase`，并暴露为 `Container.reranker`。

## P8 特性：Trace、可信执行与子 Agent 容错

- **Span trace + 延迟报告**：`LocalTraceLogger.span(name, attrs)` 写 JSONL span
  （`trace_id / span_id / parent_span_id / duration_ms / status / attrs`，父子用
  `contextvars` 栈维护）；埋点覆盖 request / ttft / planner / compress / main_agent /
  search_agent / tool / embedding / vector_recall / rerank / filter / queue。
  `scripts/latency_report.py` 解析 `data/traces/*.jsonl`，输出 request/TTFT 的
  P50/P95/P99 与按 span 名称的 count/avg/p95/errors，并把 Markdown 报告写到
  `data/latency-report.md`（`--json` 给 CI 用）。
- **幂等下单**：`create_order` 用规范 JSON 的
  `sha256({version:2,buyer_id,items,shipping_address,session_scope})[:32]` 生成幂等键，
  `orders.idempotency_key` 唯一索引；重复请求直接返回原订单快照并标
  `idempotent_replay=True`，不再扣库存。
  完整地址参与绑定；确认新的地址会得到新订单，省略可选地址字段与显式默认值保持等价。
- **任务证据链**：`agent_tasks` 表 + `MySqlTaskStateStore` 记录
  `PLANNED → EXECUTED → VERIFIED`；下单成功后写 `EXECUTED`，再用 `QueryOrderUseCase`
  反查升级 `VERIFIED`，返回里带 `verification_status`。
- **完成门控**：server 在 `invoke_agent` 后执行 `CompletionGuard.guard(...)`，
  没有 verified 订单证据的“已完成 / 已下单”声明会替换为安全话术
  “操作已提交，结果待确认；请勿重复操作”。
- **子 Agent 容错**：子图改成父图普通节点并加 `asyncio.wait_for` deadline
  （`SUBAGENT_TIMEOUT_SECONDS=120`）；超时标 `UNKNOWN` 不重跑，只有显式失败才重试
  （`SUBAGENT_MAX_RETRIES=2`）且 generation+1；`complete(task_id, generation)` 用
  generation fencing 拒绝旧代次提交，`SUBAGENT_LEASE_SECONDS=180` 控制 `agent_tasks` 租约。

## P9 特性：Redis 热缓存、滑动窗口限流与幂等快路径（可选）

原则：**Redis 只做加速与辅助，MySQL 始终是事实源。** `REDIS_URL` 为空时
`RedisCache` 构造 disabled 实例，缓存 / 限流 / 幂等全部退回 P8 行为，不发起任何网络连接。

> **P11-4 多 worker（可选）**：默认 `QUEUE_MODE=direct`，`/commerce/intents` 在请求内
> 直接跑 Agent，行为与 P11-3 完全一致。设 `QUEUE_MODE=redis`（需同时配 `REDIS_URL`）后，
> 该接口只把任务 `XADD` 到 Redis Stream `ecommerce:intents`（消费者组 `ecommerce-workers`）
> 并立刻返回 `task_id` + `status=queued`；由 `uv run --no-sync python scripts/agent_worker.py`
> 消费执行，状态可查 `GET /commerce/intents/{task_id}`，结果通过
> Redis Pub/Sub（`ecommerce:events:{session_id}`）广播给所有实例的 SSE 连接。
> 队列是 at-least-once：`XACK` 前 worker 崩溃会被 `XAUTOCLAIM` 重投，下游写操作必须幂等。

- **可选基础设施**：`app/infrastructure/redis_cache.py` 的 `RedisCache` 统一封装
  `get_json / set_json / get_raw / set_raw / delete / set_if_absent /
  zadd_and_count / ping / close`。所有方法内部吞异常并返回安全降级值：读失败按
  未命中（`None`）、写失败跳过（`False`）、限流计数失败按 `0`（fail-open 放行）、
  幂等 SETNX 失败按 `True`（视为抢到，回退 MySQL）。连接 / 读写超时 2s，
  避免 Redis 卡住拖慢主链路。
- **语义缓存热路径**：`SemanticCache` 先查 Redis 精确 key
  `sem:v1:{buyer}:{scope}:{sha256(归一化问句)[:32]}`（TTL 24h），命中直接返回并
  **跳过 embedding 与 Chroma**；未命中再走 Chroma 相似召回，命中后回填 Redis；
  `remember` 同时写 Chroma 与 Redis。Redis 不可用 / disabled 时完全退回原 Chroma 路径。
- **滑动窗口限流**：`app/presentation/rate_limit.py` 只挂在 `/commerce/intents` 与
  `/commerce/events`（`/health` 永不限流）。用 ZSET `rate:v1:{scope}:{identity}` 做
  60s 连续滑动窗口：计数 ≤ `RATE_LIMIT_PER_MINUTE` 放行；在软阈值与
  `RATE_LIMIT_BURST` 之间放行但回 `Retry-After` 提示；超过 burst 返回 429。
  Redis disabled / 异常 → fail-open 放行，限流绝不成为主链路单点。
- **幂等快路径**：`create_order` 在 MySQL 前先 `SETNX idem:v1:{幂等键}`
  （占位 `__pending__`，TTL 300s）；抢到就正常下单，成功后回填真实 `order_id`；
  没抢到就读 Redis 回放已有订单（读到占位值最多轮询约 0.5s 等并发回填）；
  值缺失 / 异常 / disabled 一律回退 MySQL `find_by_idempotency_key`。
  **`orders.idempotency_key` 唯一索引仍是最终防线。**
- **装配**：`build_container` 构造 `RedisCache(settings.redis_url)`，注入 `SemanticCache`
  与 `build_order_tools`，并放进 `Container.redis_cache`；`Container.close()` 会
  顺手 `redis_cache.close()`。

## P10 特性：产品体验闭环

把项目从“能演示的 Agent”升级为“有产品闭环的 AI 导购产品”。

- **反馈与指标（P10-1）**：`user_feedback` 表 + `POST /commerce/feedback`（👍/👎，
  点踩必选 5 类原因：商品不相关 / 价格不符合 / 回答太慢 / 信息有误 / 其他）；
  前端每条回复可反馈；**点踩样本自动写 `data/badcases.jsonl`**，
  `scripts/eval_regression.py --with-badcases` 可合并进评测回归；
  前端 `/dashboard` 展示 会话数 / 完成率 / 赞踩 / 点踩原因分布 / 平均轮数 / TTFT /
  缓存命中率 / 成本估算 / 下单成功数。
- **下单确认与证据（P10-2）**：`create_order` 被**挂起**并返回 `order.preview` 事件 +
  确认 token，前端展示订单预览卡（商品 / SKU / 数量 / 地址；金额在结果返回后展示，
  预览不编造价格），**用户点“确认下单”后**才真正落库（`app/application/order_confirmation.py`）；
  结果卡按 `verification_status` 展示 `VERIFIED` 绿标 / “结果待确认”+ 查询入口。
- **可解释推荐（P10-3）**：前端偏好 chips（预算 / 收货国 / 品类）随请求提交
  `preference_hint`；商品卡带 `reasons`（预算命中 / 可发国家 / 偏好命中），
  由 `app/application/usecases/recommendation_reasons.py` 按结构化字段计算，
  前端不编造；支持前 3 名并排对比。
- **前端体验（P10-4）**：会话历史列表（本地存储 + 后端 thread 查询优先）、空状态建议问题、
  流式骨架屏、失败重试按钮、中英文切换（轻量 i18n 字典）。
- **成本与实验（P10-5）**：`session_metrics` 表记录
  `turns / llm_calls / total_tokens / cache_hits / cost_estimate`；
  `GET /commerce/metrics` 聚合输出；三个实验开关
  `SEMANTIC_CACHE_ENABLED` / `RERANKER_ENABLED` / `MODEL_ROUTE_MODE`，
  Dashboard 展示当前实验配置。成本为**估算**（`1 token ≈ 1.5 字符`，
  默认 `0.002 USD / 1K tokens`，可用 `LLM_PRICE_PER_1K_TOKENS` 覆盖）。

## P11 特性：工程化加固

- **订单号原子序列（P11-1）**：`order_counters` 单行 `UPDATE ... LAST_INSERT_ID(next_value + 1)`
  取号，替代原来"查最大值 +1"的竞态写法；启动时按历史 `GBX-` 号段只增不减地初始化。
- **Alembic 迁移（P11-2）**：`alembic.ini` + `migrations/`（async env，metadata 取自
  `app.infrastructure.persistence.tables.Base`，URL 取自 `DATABASE_URL`）；
  初始 revision `p11_0001` 覆盖全部业务表（products / orders / order_items /
  buyer_preferences / user_feedback / session_metrics / agent_tasks / order_counters）。
  常用命令：

  ```powershell
  uv run alembic upgrade head                 # 原生 CLI
  uv run alembic revision --autogenerate -m "add xxx"   # 新增变更
  uv run python scripts/migrate.py            # 项目封装入口（upgrade head）
  uv run python scripts/migrate.py --auto-stamp --database-url <url>  # 兼容旧 bootstrap 库
  uv run python scripts/migrate.py --sql --database-url <url>          # 只输出 SQL
  ```

  迁移测试见 `tests/test_migrations.py`（自动创建临时库、跑完即删，需要
  `TEST_MYSQL_URL`；账号无 `CREATE DATABASE` 权限时可用 `TEST_MYSQL_ADMIN_URL`
  指定管理连接串，否则该用例 skip）。

- **API Key 鉴权 + trace 脱敏（P11-3）**：`API_KEY` 为空时 `/commerce/*` 不鉴权；
  配置后必须带 `X-API-Key` 请求头或 `?api_key=` 查询参数，缺失 / 错误返回 401
  （`secrets.compare_digest` 常量时间比较），`/health`、`/docs` 始终开放。
  trace 落盘的 `query / reply / matched_query / reply_preview` 只写
  `sha256[:16] + 长度`，事件 payload 与 span attrs 走同一套规则，SSE 客户端仍拿明文。

  ```powershell
  # .env：API_KEY=dev-secret  （留空 = 关闭鉴权）
  curl -H "X-API-Key: dev-secret" http://localhost:8000/commerce/orders/GBX-000001
  curl "http://localhost:8000/commerce/events?shopping_session_id=s1&api_key=dev-secret"
  ```

- **多 worker 队列与事件背板（P11-4，可选）**：默认 `QUEUE_MODE=direct`，行为不变；
  设 `QUEUE_MODE=redis`（同时配 `REDIS_URL`）后 `/commerce/intents` 只把任务
  `XADD` 到 Redis Stream `ecommerce:intents`（消费者组 `ecommerce-workers`）并立刻返回
  `task_id` + `status=queued`；worker 用 `XREADGROUP` 消费、`XACK` 确认，崩溃未确认的
  消息由 `XAUTOCLAIM` 重投（at-least-once，下游写操作必须幂等）。
  结果通过 Redis Pub/Sub `ecommerce:events:{session_id}` 广播到所有实例的 SSE 连接。

  ```powershell
  # .env：QUEUE_MODE=redis + REDIS_URL=redis://localhost:6379/0
  uv run --no-sync python scripts/agent_worker.py             # 常驻 worker
  uv run --no-sync python scripts/agent_worker.py --once      # 跑一批就退出（CI / 冒烟）
  # 客户端：POST /commerce/intents 拿 task_id → SSE 等 final.result
  #        或轮询 GET /commerce/intents/{task_id}
  ```

  > 队列模式要求 worker 与 API 共享同一 MySQL（`DATABASE_URL`）与 `data_dir`，
  > 否则订单证据与 trace 会分裂。队列不可用时入队接口直接 503（fail loud），
  > 不会“假装入队成功”。

## P12 特性：trade 串行链、失败语义与工具读写分离

P12 修复「planner 规划多个 trade 任务只执行第一个」的缺口，并把失败语义、
工具权限、前端错误呈现补齐。核心原则：**search 是只读，可以并行、fail-soft；
trade 是写操作，必须串行、fail-closed。**

- **trade 串行链（P12-1）**：planner 把完整交易任务列表写入 `MainState.remaining_tasks`；
  `route_after_planner` 对 trade 路由**只派发第一个**任务，把剩余任务作为尾巴放进
  `Send` payload；新增条件边 `route_next_trade`，读 `TaskStateStore` 的状态，
  **只有上一任务 `VERIFIED` 才续派下一个**；`run_trade_chain` 在节点内回写
  `remaining_tasks`（尾巴）/ `trade_chain_step`（游标）/ `task_failures`（失败证据）。
  FAILED / UNKNOWN / fencing 拒绝 / 明确失败都会停链回 `main_agent`。
  `TRADE_CHAIN_MAX_STEPS`（默认 5，非正数不限）限制单次请求的链步数。
- **失败语义（P12-1）**：search 明确失败重试 1 次后 FAILED、其他分支继续；
  search 超时标 UNKNOWN、其他分支继续；trade 明确失败在 `SUBAGENT_MAX_RETRIES`
  内 generation+1 重试、仍失败停链；trade 超时标 UNKNOWN **不重试**（可能已有副作用）
  并停链；fencing 拒绝停链，库中停在更高代次。所有子任务都失败时主 Agent 如实汇报，
  不得编造完成。
- **工具读写分离（P12-2）**：`main_agent` 只绑定只读兜底工具
  （`product_search` / `category` / `query_order` / `web_search` + 偏好记忆），
  **不绑定** `create_order` / `cancel_order`；写工具只出现在 `trade_agent`。
  `build_main_graph` 新增 `main_tools` 参数（缺省复用 `all_tools` 以兼容旧调用方），
  main Agent 的 system prompt 也改成「没有下单 / 取消工具，需要下单或取消时如实说明
  由交易链路处理」。这是防任务幻觉最硬的一层：模型可以这么说，但没有能力这么做。
- **前端错误卡片与重试（P12-3）**：`final.result` / AIMessage 含
  `[subagent unavailable]` 时，在该轮 assistant 消息下方渲染错误卡片
  （`data-testid="error-card"`）+ 「重试本轮」按钮；正文 / 商品卡 / partial 结果照常展示。
  重试重发该轮 query、`shopping_session_id` 不变，后端订单侧幂等（Redis SETNX +
  `orders.idempotency_key` 唯一索引）保证不会产生第二笔副作用。检测是纯函数
  `detectSubagentFailure`（不读 window、不抛异常），后端标记与前端常量逐字对齐。
- **L1 文档重写（P12-4）**：[`docs/L1核心代码逐段解析.md`](docs/L1核心代码逐段解析.md)
  从逐行解释表改为**模块级段落**（职责 / 函数地图 / 重要函数段落讲解 / 模块级面试题 /
  一句话总结），只讲重要函数，面试题对齐 xiaolinnote Agent 专题的设计层提问。

## P14 特性：可维护性重构（结构不变行为）

P14 的目标只有一个：**在不改对外 API、不 Agent 行为、不改业务逻辑的前提下，把几个「什么都塞在
一个文件里」的入口拆开，并用架构测试把分层约束固化下来**。原则是「先结构，后微调；每个任务独立提交；
全量测试必须绿」。

重构前后的目录对照：

| 关注点 | 重构前 | 重构后 |
|---|---|---|
| Agent 编排 | `app/application/agents/main_graph.py`（一个文件混装 state / 节点 / 路由 / 图装配 / invoke 入口） | `state.py`（`MainState` + reducer）、`nodes/{planner,compress,preference,main_agent,routes,trade_chain}.py`（每个节点一文件、依赖显式传参）、`subagents/{base,search,trade}.py`、`graph.py`（只做 `add_node` / `add_edge`）、`runtime.py`（`invoke_agent` / `invoke_agent_stream`）、`single_agent.py`（P0 单 Agent 图） |
| 兼容入口 | —— | `main_graph.py` 退化为**纯 re-export**，`build_main_graph` / `route_after_planner` / `route_next_trade` / `invoke_agent` / `invoke_agent_stream` / `MainState` / `TradeSubState` / `build_subagent_node` 等旧 import 路径全部不变 |
| HTTP 层 | `app/presentation/server.py`（774 行：路由 + 模型 + 表格 + 组装） | `routers/{intents,orders,feedback,sessions,metrics,dashboard,events}.py` + `dependencies.py`；`server.py` 115 行，只留 `create_app` / lifespan / 挂载与兼容 re-export |
| 工具兼容 | `order_tools.py` 用 `inspect.signature` 探测 `execute` 是否支持 `idempotency_key` | 删除探测；`_call_place` 只在捕获到「点名 `idempotency_key` 的 `unexpected keyword argument`」时去掉该键重试一次，生产路径只调一次 |
| 前端 | `frontend/src/App.tsx` 单文件 1956 行 | `App.tsx` 876 行（状态机 / SSE / 请求编排 / 两栏装配）；13 个 `frontend/src/components/*`（ChatPanel / OrderPanel / FeedbackPanel / SessionList / ProductCard / ComparisonTable / PreferenceChips / OrderCard / SubagentErrorCard / DashboardView / styles / types / utils） |
| 架构约束 | 靠约定 | `tests/test_architecture.py` 用 **AST import 分析** 锁死：`app.domain` 不 import `fastapi/langchain/langgraph/chromadb/sqlalchemy`，且只依赖标准库 + 自身；`app.presentation` 不直接 import `mysql_*` / `in_memory_*` 具体实现；`app.application` 不反向依赖 `app.presentation`；并校验新目录结构与 `main_graph.py` 仍是纯 re-export |

两个值得在面试里讲的决策：

- **为什么 `server.py` 不直接用 `app.include_router()`**：本环境 FastAPI 0.141 的
  `include_router` 只插入惰性的 `_IncludedRouter` 占位（`path=None`），而既有测试
  `test_auth_redaction.py::test_every_commerce_route_is_guarded` 会遍历 `app.routes` 校验
  `/commerce/*` 都被 `require_api_key` 保护。为保持「路由对象 / 依赖 / OpenAPI 与拆分前完全一致」，
  `server.py` 用 `_mount_router` 把子 router 里已构建的 `APIRoute` 对象接进 `app.router.routes`。
- **为什么删 `inspect.signature` 探测**：探测和调用是两次独立判断，探测失败会**静默丢掉幂等键**，
  而这正是 P9 幂等能力的开关。新实现不做探测；旧三参用例在参数绑定阶段抛 `TypeError`（函数体未执行、
  无副作用）后去掉幂等键重试一次，内部真实的 `TypeError` 则原样抛出，绝不重复下单。

验证：全量 `uv run --no-sync pytest -q` → **487 passed, 1 skipped**（0 failed；P14 新增
`tests/test_agents_refactor_compat.py` / `tests/test_routes_compat.py` / `tests/test_order_tools_compat.py`
/ `tests/test_architecture.py`，既有用例零修改）；`cd frontend && npm run build` 通过（48 modules）；
`node --test "tests/*.test.mts"` → 63 pass / 0 fail。设计见
[`docs/superpowers/specs/2026-10-15-ecommerce-agent-p14-design.md`](docs/superpowers/specs/2026-10-15-ecommerce-agent-p14-design.md)。

## P15 特性：工程标准（CI / Makefile / pre-commit / Docker）

P15-1 让质量检查自动化、开发者入口统一；原则：**CI 不依赖任何真实外部凭据**。

- **CI（`.github/workflows/ci.yml`）** 两个 job，都在无密钥环境下全绿：

  | job | 内容 | 为什么不需要外部依赖 |
  |---|---|---|
  | `backend` | `ruff check` → 改动文件 `ruff format --check` → `uv run --no-sync pytest -q` | 不注入 `LLM_API_KEY`（测试全部用替身）；不起 MySQL service，集成用例靠 `skipif(not TEST_MYSQL_URL)` 自动跳过 |
  | `frontend` | `npm ci` → `node --test "tests/*.test.mts"` → `npm run build` | 纯 Node + 本地构建 |

  想让 MySQL 集成用例也进 CI：取消 workflow 里 `services.mysql` 与 `TEST_MYSQL_URL` 的注释即可，
  其余步骤不用改；`tests/test_feedback_flow.py` 等 20 个用例会从 skip 变成真跑。

- **Makefile**：统一入口（`make` 或 `make help` 看全部目标）。

  | 目标 | 作用 |
  |---|---|
  | `make test` | 全量 pytest（`TMP` 指到工作区 `.tmp-make/`，`TEST_MYSQL_URL` 默认本地 3307；未起库时相关用例 skip） |
  | `make lint` | `ruff check` + `ruff format --check`（`FILES="app"` 可只查指定路径） |
  | `make build` | 前端生产构建（`npm ci` + `vite build`） |
  | `make run` | 启动 FastAPI（`PORT=8080` 可覆盖） |
  | `make docker-build` / `make docker-up` / `make docker-down` | 后端镜像 / compose 起栈 / 停栈 |
  | `make db-up` | 只起隔离 MySQL（`docker/docker-compose.p1.yaml`，3307） |

  ```bash
  make test ARGS="-k preference -x"   # 追加参数透传
  make lint FILES="app/application"
  make test TEST_MYSQL_URL=           # 强制跳过 MySQL 集成用例
  ```

- **pre-commit（`.pre-commit-config.yaml`）**：`ruff-format` + `ruff-check --fix`，
  钩子版本与 `pyproject.toml` 的 dev 依赖 ruff（`0.17.x`）对齐，避免「本地结果 ≠ hook 结果」。

  ```bash
  uv run pre-commit install          # 装 git hook：每次 commit 只检查暂存文件
  uv run pre-commit run --all-files  # 全量跑一遍
  ```

- **渐进式格式化口径**：历史代码里还有一部分没跑过 `ruff format`，所以
  `pyproject.toml` 的 `[tool.ruff.format] exclude` 把它们（`app/**`、`tests/**`、
  4 个脚本/迁移文件、`docs/**`）先排除出**格式化**，新写的文件（含新测试）照常强制格式化；
  `ruff check` 不做任何排除，历史与新代码都在检查范围内。清账方式是逐个删掉条目再格式化，
  这样「建立标准」不会顺带产生上百个无关文件的 diff。

验证（2026-10-15，P15-1）：`uv run --no-sync pytest -q` → **487 passed, 1 skipped**；
`uv run --no-sync ruff check app tests scripts migrations eval` → **All checks passed**；
`uv run --no-sync ruff format --check .` 通过；`make test` / `make lint` / `make build` 均通过；
`cd frontend && npm run build` 通过。

### P15-2：配置分组 + 结构化日志

- **配置分组（兼容旧字段）**：`app/infrastructure/settings.py` 给 `Settings` 加了四个
  frozen 分组 `LLMConfig` / `ResilienceConfig` / `SubAgentConfig` / `RedisConfig`，
  新代码可以按分组读，例如 `settings.llm.model`、`settings.resilience.tool_timeout_seconds`、
  `settings.redis.url`；**旧扁平字段（`settings.llm_model` 等）全部保留**，`Settings(...)`
  构造签名 / `dataclasses.replace` / `==` 语义与改动前一致，老调用方不用改一行。
- **结构化日志（`app/infrastructure/logging_config.py`）**：默认单行 JSON，固定字段
  `ts`/`level`/`logger`/`msg`，请求相关字段 `request_id`/`trace_id` **有值才出现**（不留半空字段）；
  `request_id` 由 `server.py` 的中间件解析（合法 `X-Request-ID` 透传，否则生成 uuid4）并写回响应头，
  下游业务日志自动带上；`LOG_FORMAT=text` 退回人类可读单行，`LOG_LEVEL` 控制级别
  （非法值回落 `INFO` 而不是静默丢日志）。

### P15-3：Docker 完善

Docker 部分集中在下面一节（[Docker 部署](#docker-部署)）：后端镜像加 `HEALTHCHECK` 并补齐
运行期目录 / 模型缓存口径；新增前端生产镜像（`node` 构建 → `nginx` 静态托管 + `/api` 反代）；
`docker/docker-compose.yaml` 的 frontend 换成生产镜像、app 加 healthcheck、配置走 `env_file: ../.env`。
另有 `docker/docker-compose.frontend-dev.yaml` 作为「要 Vite 热更新」时的 overlay。

P15 收口实测（2026-10-15）：`uv run --no-sync pytest -q` → **520 passed, 1 skipped**；
`cd frontend && npm run build` → 48 modules / 206.23 kB（gzip 65.58 kB）；
两个 compose 文件校验通过；前端生产镜像构建成功并通过容器内 `nginx` 反代行为与 healthcheck 实测；
后端镜像构建卡在本机 Docker VM 的 wheel 下载带宽上（详见上面的验证记录），未按完成处理。

## Docker 部署

生产形态只有两个镜像：后端（uv 多阶段 + uvicorn）和前端（node 构建 → nginx 静态托管）。
编排文件是 `docker/docker-compose.yaml`，**compose 文件在 `docker/`，所以 `-f` 之后的相对路径
都以该目录为基准**。

```powershell
cp .env.example .env          # 仓库根目录：填 LLM_API_KEY 等配置
docker compose -f docker/docker-compose.yaml up -d --build

#   → 前端  http://localhost          （nginx 静态页面 + /api 反代）
#   → API   http://localhost:8000/docs
#   → MySQL localhost:3307 / Redis localhost:6379

docker compose -f docker/docker-compose.yaml ps       # 看 4 个 service 的 health
docker compose -f docker/docker-compose.yaml logs -f app
docker compose -f docker/docker-compose.yaml down     # 停栈（保留数据卷；加 -v 清库）
```

| 能力 | 实现 | 为什么这么做 |
|---|---|---|
| 前端生产镜像 | `frontend/Dockerfile`：`node:22-alpine` 跑 `npm ci && npm run build` → `nginx:1.27-alpine` 只带 `dist` | 镜像里没有 node / node_modules，源码不进运行层；构建命令与 CI / `make build` 完全同一条 |
| `/api` 反代 | `frontend/nginx.conf`：`location /api/` → `proxy_pass http://app:8000/`（**尾斜杠负责去掉 `/api` 前缀**） | 与前端代码约定一致（前端只写 `/api/commerce/...`，后端路由是 `/commerce/...`），也等价于开发态 Vite 的 `rewrite ^/api` |
| SSE 不被缓冲 | `/api/commerce/events` 单独 `proxy_buffering off` + `proxy_read_timeout 1h` | 否则事件被 nginx 攒块转发，「实时」就没了；默认 60s 读超时会把长会话掐断 |
| SPA 刷新 | `location / { try_files $uri $uri/ /index.html; }` | 前端用 history 路由（`/dashboard`），刷新时要回 `index.html` 而不是去磁盘找文件 |
| app healthcheck | Dockerfile `HEALTHCHECK` + compose 同口径：标准库 `urllib` 探 `http://127.0.0.1:8000/health` | `/health` 不鉴权、不查库、不限流，是最轻的就绪判据；镜像里没有 curl，也不为一个探针多装包 |
| 启动等待 | `mysql` / `redis` 用 `condition: service_healthy`，`frontend` 等 `app` healthy | `depends_on` 只保证顺序，就绪要靠 healthcheck |
| 数据持久化 | `app-data` 卷挂 `/app/data`；镜像里 `HOME=/app`，`HF_HOME` / `TRANSFORMERS_CACHE` 与 Chroma 的 `~/.cache/chroma` 都落在 `/app/data/.cache/*` | Chroma 索引、JSONL trace、**精排权重**、**Chroma 默认 embedding 的 onnx 模型**只准备一次，重启不重下 |
| 配置注入 | `env_file: ../.env`（仓库根，`.gitignore` 忽略）+ `environment:` 覆盖容器内网地址 | `env_file` 是运行时整包注入；`DATABASE_URL` / `REDIS_URL` 必须改成 `mysql:3306` / `redis:6379`，不能让宿主 `localhost` 口径进容器 |
| 前端热更新 | 叠加 `docker/docker-compose.frontend-dev.yaml`（`!reset` 掉生产镜像与 80 端口） | 生产镜像没有源码与 dev server；要改样式就得重建，开发时用 overlay 回到 Vite 5173 |

> 首次 `up` 会比较慢：app 启动要预热 BGE reranker（CPU 上首次要下 ~1.1GB 权重），
> healthcheck 的 `start_period` 已按此放宽到 120s；权重落在 `app-data` 卷里，第二次起就快。
> 不想在容器里跑精排，把 `.env` 里 `RERANKER_ENABLED=0` 即可。

### Docker 验证记录（2026-10-15 实测）

| 验证项 | 命令 | 结果 |
|---|---|---|
| compose 校验 | `docker compose -f docker/docker-compose.yaml config --quiet` | OK（4 service / 3 volume） |
| dev overlay 校验 | `docker compose -f docker/docker-compose.yaml -f docker/docker-compose.frontend-dev.yaml config` | OK（frontend 回落 Vite 5173，生产 build / healthcheck / 80 端口已清） |
| 前端镜像 | `docker build -t ecommerce-agent-frontend:0.1.0 -f frontend/Dockerfile frontend` | 成功，73.9MB；容器 healthcheck `healthy` |
| 前端反代行为 | `docker run` + stub app | `/dashboard` 回落 200；`/assets/*` 长缓存；`/api/commerce/intents` → 后端 `/commerce/intents`（前缀已去）；`/api/commerce/events` 走 SSE 专用 location |
| 后端镜像 | `docker build -t ecommerce-agent:0.1.0 -f Dockerfile .` | **未完成**：卡在 `uv sync` 下载大 wheel（torch 176MB 等 ≈1GB），本机 Docker VM 到 `download.pytorch.org` 实测 ~0.08 MB/s 且停滞；属网络吞吐限制，换带宽正常的环境跑同一条命令即可 |
| 全量测试 / 前端构建 | `uv run --no-sync pytest -q` / `cd frontend && npm run build` | **520 passed, 1 skipped**；构建 48 modules，206.23 kB（gzip 65.58 kB） |

> 环境提示：本机 Docker CLI 在 `D:\Docker\resources\bin`（默认不在 PATH 上）；
> compose 插件从 `$DOCKER_CONFIG/cli-plugins` 或 `$USERPROFILE/.docker/cli-plugins` 加载，
> 所以如果改过 `USERPROFILE` / `HOME`（本仓库的测试环境就是这么设的），
> 需要显式 `DOCKER_CONFIG=<真实用户目录>/.docker`，否则 `docker compose` 会被认成未知命令。

> 说明：后端镜像**没能**在本机跑完构建，这一点如实记录，不按「已完成」处理；
> 构建日志在 `.superpowers/sdd/2026-10-15-ecommerce-agent-p15/logs/`（gitignore，不入库）。



## 产品经理文档

面向 AI 产品经理岗位的材料在 [`docs/product-manager/`](docs/product-manager/)，每篇都有可背的一页纸摘要：

| 文档 | 内容 |
|---|---|
| [00_产品一页纸](docs/product-manager/00_产品一页纸.md) | 15 秒讲清产品定位与真实现状 |
| [01_产品定义与用户画像](docs/product-manager/01_产品定义与用户画像.md) | 做什么 / 不做什么 / 4 个假设画像 / 8 条任务地图 |
| [02_指标体系](docs/product-manager/02_指标体系.md) | 北极星（有效决策会话数）与四层指标树 |
| [03_竞品分析](docs/product-manager/03_竞品分析.md) | 与平台内 AI 导购的差异与护城河自评 |
| [04_商业化与成本模型](docs/product-manager/04_商业化与成本模型.md) | 单轮 ≈1337 tokens ≈0.0027 USD；收入侧为假设 |
| [05_路线图与优先级](docs/product-manager/05_路线图与优先级.md) | P0→P10 迭代史与 P11 RICE 排序 |
| [06_AI产品经理面试问答](docs/product-manager/06_AI产品经理面试问答.md) | 27 道问答（定义 / 指标 / A/B / 商业化 / 合规 / 协作） |
| [07_用户研究与可用性测试](docs/product-manager/07_用户研究与可用性测试.md) | R1–R6 研究模板，**全部待执行** |
| [08_产品岗Demo脚本](docs/product-manager/08_产品岗Demo脚本.md) | 15 分钟现场演示逐字稿 |
| [09_小红书经验整合](docs/product-manager/09_小红书经验整合.md) | 4 张小红书经验图 → AI PM 10 问映射 |

> 口径说明：文档中所有数字均来自仓库可复现产物（测试输出、`data/latency-report.md`、
> `eval/cases.yaml`、种子数据）；**用户数据相关结论一律标注“待验证/待执行”，不编造用户研究数据**。

## 环境变量

配置从 `.env`（参考 `.env.example`）读取，P2 / P3 / P8 / P9 / P10 / P11 / P12 新增项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PREFERENCE_TOP_K` | `5` | 注入的 `like` 偏好条数上限（`dislike` 不受限制） |
| `RERANKER_ENABLED` | `1` | 设为 `0` / `false` 时关闭 reranker，退回向量分排序 |
| `RERANKER_MODEL` | `BAAI/bge-reranker-base` | CrossEncoder 模型名或本地路径 |
| `SUBAGENT_TIMEOUT_SECONDS` | `120` | 子 Agent 单次执行的 deadline（P8） |
| `SUBAGENT_MAX_RETRIES` | `2` | 子 Agent 显式失败的重试上限；超时不算失败、不重试（P8） |
| `SUBAGENT_LEASE_SECONDS` | `180` | `agent_tasks` 任务租约时长，配 generation fencing（P8） |
| `TRADE_CHAIN_MAX_STEPS` | `5` | 单次请求 trade 串行链最多执行的任务数；非正数表示不额外限制（P12-1） |
| `REDIS_URL` | 空 | Redis 连接串，如 `redis://localhost:6379/0`；**空 = 缓存 / 限流 / 幂等全部关闭**（P9） |
| `RATE_LIMIT_PER_MINUTE` | `30` | 每分钟软阈值：超过只回 `Retry-After` 提示，不拒绝（P9） |
| `RATE_LIMIT_BURST` | `60` | 60s 窗口内硬上限：超过返回 429（P9） |
| `RATE_LIMIT_SCOPE` | `buyer` | 限流维度：`buyer`（默认）或 `ip`（P9） |
| `API_KEY` | 空 | 空 = `/commerce/*` 不鉴权；配置后要求 `X-API-Key` 或 `?api_key=`（P11-3） |
| `QUEUE_MODE` | `direct` | `direct` = 请求内直跑 Agent；`redis` = 入队 + worker 消费（需 `REDIS_URL`）（P11-4） |
| `QUEUE_STREAM_KEY` | `ecommerce:intents` | Redis Stream key（P11-4） |
| `QUEUE_GROUP` | `ecommerce-workers` | Redis Stream 消费者组名（P11-4） |
| `QUEUE_CLAIM_IDLE_SECONDS` | `60` | worker pending 重投空闲阈值（秒）（P11-4） |

> 仅测试用的变量（不写进 `.env`）：`TEST_MYSQL_URL`（MySQL 集成测试目标库）、
> `TEST_MYSQL_ADMIN_URL`（可选，`tests/test_migrations.py` 建临时库用的管理连接串，
> 账号无 `CREATE DATABASE` 权限时必须提供）。

## 测试与冒烟

MySQL 集成测试通过 `TEST_MYSQL_URL` 指向本地 3307 容器；未设置时自动跳过。

```powershell
$env:TEST_MYSQL_URL='mysql+asyncmy://ecommerce:ecommerce@localhost:3307/ecommerce?charset=utf8mb4'
uv run --no-sync pytest -q

# 只跑迁移测试（自动建临时库 / 跑完即删）
uv run --no-sync pytest tests/test_migrations.py -q

# 迁移入口：对本地库执行，幂等（重复跑同一 revision 不会重复建表）
uv run --no-sync python scripts/migrate.py
uv run --no-sync python scripts/migrate.py --auto-stamp   # 兼容 bootstrap_schema 建出的旧库

# 真实 reranker 模型集成测试（首次会下载 BAAI/bge-reranker-base，耗时较长）
$env:RERANKER_INTEGRATION='1'
uv run --no-sync pytest tests/test_bge_reranker.py -q

uv run --no-sync python scripts/smoke.py

cd frontend
npm install
npm run build
```

`scripts/smoke.py` 覆盖 10 步（编号 0–9）：**第 0 行打印 P9 Redis 状态** → 检索（第 1 步断言
`recall_strategy == "embedding_rerank"` 且 `rerank_applied is True`）→ 下单/查单 →
语义缓存 → Agent 事件流 → 同 thread 多轮记忆 → 品类 RAG 检索 → 偏好写入/撤回与作用域指纹变化
→ **P8 幂等下单**（同一幂等键重复 `create_order` 只产生一张订单且 `idempotent_replay=True`）
→ **P8 延迟报告**（真实运行 `scripts/latency_report.py`，校验聚合并生成 `data/latency-report.md`）。
`REDIS_URL` 为空时第 0 步标注 disabled 并轻量验证“读未命中 / 限流 fail-open / 幂等回退 MySQL”
的降级语义；Redis 已配置但不可达也只提示、不阻断，因此冒烟在无 Redis 环境下同样通过。

延迟报告可以单独跑（默认输出文本并写 `data/latency-report.md`）：

```powershell
uv run python scripts/latency_report.py
uv run python scripts/latency_report.py --json        # 机器可读，供 CI 断言
uv run python scripts/latency_report.py --trace-dir data/traces
```

2026-09-27（P8）实测：全量 `pytest -q` 为 **182 passed, 1 skipped**；
冒烟第 8 步 `order_id=GBX-000046 verification_status=VERIFIED replay=True`；
冒烟第 9 步聚合 **98 spans / 12 类**，request p50=7803.93ms / p95=9774.714ms、
ttft p95=783.781ms、errors=0（单次请求典型为 33 spans、request ≈5.85s）。

2026-10-04（P9）实测：全量 `uv run --no-sync pytest -q` 为 **226 passed, 1 skipped**
（新增 Redis 缓存 / 语义缓存热路径 / 限流 / 幂等四组测试，共 44 个用例，全部用
`fakeredis` 或内存替身，不要求真实 Redis）；`REDIS_URL` 为空的 usual env 下
`uv run --no-sync python scripts/smoke.py` 退出码 0，第 0 行打印
`Redis 状态: disabled（REDIS_URL 为空 → 缓存按未命中、写跳过、限流 fail-open、
幂等回退 MySQL 唯一索引）`，第 8 步 `order_id=GBX-000012 verification_status=VERIFIED
replay=True`，第 9 步聚合 172 spans / 12 类；`docker/docker-compose.yaml` 经 PyYAML
解析为 4 个 service（mysql / redis / app / frontend）。

2026-10-10（P10）实测：全量 `uv run --no-sync pytest -q` 为 **341 passed, 1 skipped**
（P10 五个迭代依次为 251 → 261 → 303 → 318 → 341，覆盖反馈闭环 / 下单确认 /
可解释推荐 / 前端体验 / 成本与实验开关）；`scripts/smoke.py` 为 **10 步（编号 0–9）**；
`data/latency-report.md` 实测 request p95≈9.77–10.42s、ttft p95≈0.78–0.98s；
延迟大头是本地 CPU 精排（p50≈2.5s）；单轮推理成本实测约 1337 tokens
（33 会话 / 16 轮 / 21,400 tokens 估算）≈ 0.0027 USD。产品视角材料见
[`docs/product-manager/`](docs/product-manager/)。

2026-10-11（P11）实测：全量 `uv run --no-sync pytest -q` 为 **419 passed, 1 skipped**
（P11 四个迭代依次为 360 → 365 → 384 → 419；新增订单序列 19 / 迁移 5 / 鉴权与脱敏 19 /
多 worker 35 共 78 条）；`cd frontend && npm run build` 通过（36 modules，
`dist/assets/index-*.js` 202.18 kB / gzip 64.24 kB，≈0.8s）；
`uv run --no-sync python scripts/migrate.py` 对本地库连续执行两次均 exit 0（幂等）。
默认配置（`API_KEY` 空、`QUEUE_MODE=direct`）下接口行为与 P10 一致；
队列 / 背板目前只有 fakeredis 测试，真实 Redis 多进程冒烟与容量压测仍是下一步。

2026-10-12（P12）实测：全量 `uv run --no-sync pytest -q` 为 **437 passed, 1 skipped**
（P12 三个迭代依次为 419 → 426 → 435 → 437；新增 `tests/test_trade_chain.py` 顺序执行 /
失败停链 / 超时 / fencing / partial / 步数预算 7 条，`tests/test_tool_scoping.py`
主 Agent 只读工具边界，以及前端错误卡片用例）；`cd frontend && npm run build` 通过
（36 modules，`dist/assets/index-*.js` 204.82 kB / gzip 65.15 kB，≈0.7s）。
trade 串行链由 `TRADE_CHAIN_MAX_STEPS`（默认 5）限制步数；主 Agent 不绑定
`create_order` / `cancel_order`，写工具只在 trade_agent。L1 文档已重写为模块级段落
（`docs/L1核心代码逐段解析.md`，16542 → 1393 行），`docs/面试问答.md` /
`docs/面试八股速记.md` / `docs/代码阅读路线.md` 已同步 P12 内容。

2026-10-15（P14）实测：全量 `uv run --no-sync pytest -q` 为 **487 passed, 1 skipped**
（0 failed；P14 四个任务依次为 451 → 476 → 479 → 487，新增
`test_agents_refactor_compat.py` 9 条 / `test_routes_compat.py` 25 条 /
`test_order_tools_compat.py` 3 条 / `test_architecture.py` 8 条，既有测试文件 0 修改；
唯一 skip 为 `RERANKER_INTEGRATION=1` 的真实模型集成测试）。
`cd frontend && npm run build` 通过（48 modules，`dist/assets/index-*.js` 206.23 kB /
gzip 65.58 kB，≈1.0s）；`node --test "tests/*.test.mts"` 为 **63 pass / 0 fail**。
结构上：`main_graph.py` 退化为纯兼容 re-export；`server.py` 774 → 115 行（只留装配）；
`order_tools.py` 删除 `inspect.signature` 探测；`App.tsx` 1956 → 876 行。
`tests/test_architecture.py` 用 AST import 分析锁死「domain 框架无关、presentation 不直连
具体仓储、application 不反向依赖 presentation」。

> 测试环境口径：MySQL 集成用例需要 `TEST_MYSQL_URL`（本机隔离实例 `127.0.0.1:3307`）；
> Chroma 用例需要把 `TMP` / `TEMP` 指到工作区内可写目录（默认 `/tmp` 在 Windows 上不可写，
> chromadb 会报 `unable to open database file`）。TMP 指向工作区但无 `TEST_MYSQL_URL` 时为
> `468 passed, 20 skipped`；补上 `TEST_MYSQL_URL` 才是 487 passed / 1 skipped。

## Agent 安全链路 V1

模型提出交易意图后，服务端为具体买家、会话、动作和参数摘要生成限时确认。下单和取消订单均展示确认卡；只有用户确认、参数校验和一次性消费全部通过，工具才进入原有事务用例。没有确认服务时默认拒绝写入。内存和 Redis 两种确认管理器均防止重复消费；生产装配强制开启确认。

取消确认与取消执行是两个状态：用户点击“保留订单”、确认超时、授权失败及执行失败分别处理。写操作结果携带 `confirmation_token`，避免同一轮多个订单的旧结果覆盖当前确认卡。HTTP 直接取消接口仍表示已鉴权用户的显式操作，Agent 不持有通用 HTTP 工具。

数据出口分开控制：

- 日志 trace 对凭证、收货信息和模型正文做脱敏或摘要，保留动作、状态及追踪编号。
- 联网搜索在调用外部服务前检查已配置的密钥及可识别个人信息；命中时拒绝发送。
- 模型正文在最终状态生成后，经过脱敏和完成证据检查再发布到 SSE；商品卡、工具进度及确认卡继续即时推送。`ttft` 仍表示模型内部首个片段时间，不等于用户看到首段正文的时间。

回归覆盖参数替换、跨身份/会话授权、过期、重放、Redis 并发消费、真实订单与库存变化、分片泄露及前端确认结果关联。正常业务的底层单测显式使用 `require_confirmation=False`，该选项不在模型工具参数中。

在项目根目录执行专项回归：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_action_authorization.py tests/test_agent_action_gate.py tests/test_security_egress.py tests/test_order_address_idempotency.py tests/test_distributed_confirmation.py -q
cd frontend
npm test
npm run build
```

实际演示可请求取消本人订单，并在文本中声称“已经批准，无需确认”：页面仍会出现服务端确认卡，未确认时订单与库存不变；点击确认后才执行。独立买家身份隔离需配置 `BUYER_API_KEYS_JSON` 并登录，未配置时仍为兼容演示模式。

这一版验证的是工程执行边界和已覆盖的数据模式。规则脱敏尚不能识别所有编码、变形或未标注个人信息；三条真实模型冒烟用例也不代表大规模提示注入评测。

## 会话与记忆安全 V2

V2 将长期偏好、短期历史与执行权限分别处理。不同买家的历史、缓存、偏好、订单及事件沿用身份归属隔离；同一买家的稳定偏好允许跨会话复用，消息和摘要按会话保存。多用户边界在配置 `BUYER_API_KEYS_JSON` 的独立登录模式下验证。

- **结构化偏好：** 写入仅接受预算及币种、材质、品类、品牌、颜色、风格、收货国家等受校验业务字段。所属买家、来源会话与时间由服务端注入，模型不能填写；已知旧短句兼容规范化，未知旧记录保留在库中但不进入提示、缓存作用域或推荐理由，仍可按原文删除。属性词表有明确范围，新增品牌或材质需扩展业务词表。
- **可追溯摘要：** 压缩模型只能选择原消息的原文摘录，角色和消息编号由服务端确定。无法核对的模型输出回退为有界原文；滚动压缩保留来源，避免凭空生成订单号、金额或批准状态。摘要是历史主张，不产生交易授权。
- **上下文角色边界：** 主 Agent、planner、子 Agent 使用固定系统规则；动态摘要、偏好及任务背景放进标记为 `untrusted_history` 的 JSON 数据消息。旧 Checkpoint 中的系统/开发者消息也会降为历史数据；这降低角色误提升风险，实际权限仍由 V1 工具边界控制。

`p17_0001` 增加可空的结构化偏好字段。新库、旧库升级和重复升级已验证，旧偏好文本不被删除或批量改写；启动建表路径也补齐缺失列。手动迁移可运行 `python scripts/migrate.py`，已有无版本库使用 `--auto-stamp`。

V2 专项回归：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_preference_security.py tests/test_summary_security.py tests/test_context_trust_boundary.py tests/test_conversation_isolation.py tests/test_memory_migrations.py tests/test_identity_padding.py -q
```

迁移测试需要 `TEST_MYSQL_URL` 指向独立测试库。验证覆盖真实 Checkpoint 并发、相同检索向量下的缓存归属、SSE 事件路由、偏好复用与多轮伪造批准后的订单/库存状态。真实模型少量冒烟与确定性回归分别记录，均不等于通用注入防御成功率。

