# -*- coding: utf-8 -*-
"""组装容器：数据库/检索/订单用例/工具/Agent 一次装配。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.application.agents.main_graph import build_main_graph
from app.application.metrics.session_metrics import experiment_config_from_settings
from app.application.order_confirmation import OrderConfirmationManager
from app.application.tools.category_insight_tool import build_category_insight_tool
from app.application.tools.order_tools import build_order_tools
from app.application.tools.preference_tools import build_preference_tools
from app.application.tools.product_search import build_product_search_tool
from app.application.tools.web_search_tool import build_web_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.domain.order.order_id import format_order_id
from app.infrastructure.cache.chroma_semantic_cache import DisabledSemanticCache, SemanticCache
from app.infrastructure.checkpoint import build_checkpointer
from app.infrastructure.embedding.chroma_embedding_client import ChromaLocalEmbeddingClient
from app.infrastructure.eventbackplane import RedisEventBackplane
from app.infrastructure.eventbroker import EventBroker
from app.infrastructure.llm import build_chat_model
from app.infrastructure.persistence.badcase_store import JsonlBadcaseWriter
from app.infrastructure.persistence.db import bootstrap_schema, create_engine
from app.infrastructure.persistence.mysql_feedback_store import MySqlFeedbackStore
from app.infrastructure.persistence.mysql_metrics_store import MySqlMetricsStore
from app.infrastructure.persistence.mysql_preference_store import MySqlPreferenceStore
from app.infrastructure.persistence.mysql_repositories import (
    MySqlOrderRepository,
    MySqlProductRepository,
    seed_order_counter,
)
from app.infrastructure.persistence.mysql_task_store import MySqlTaskStateStore
from app.infrastructure.persistence.seed_products import build_seed_products
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue
from app.infrastructure.rag.category_knowledge import CategoryKnowledgeBase
from app.infrastructure.redis_cache import RedisCache
from app.infrastructure.rerank.bge_reranker import BgeReranker
from app.infrastructure.resilience import CircuitBreakerRegistry, build_resilient_tool
from app.infrastructure.settings import PROJECT_ROOT, Settings
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.tracing import LocalTraceLogger
from app.infrastructure.vector.chroma_product_index import ChromaProductIndex
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.websearch.tavily_search import TavilySearchClient

logger = logging.getLogger(__name__)

#: create_order 韧性包装相对确认超时的缓冲秒数：保证 manager.wait 先超时返回取消结果，
#: 而不是被外层 asyncio.wait_for 先掐断成 error。
ORDER_CONFIRM_TIMEOUT_BUFFER_SECONDS = 15.0


@dataclass
class Container:
    """装配容器：持有仓储、用例、工具、Agent 与各类基础设施句柄。

    每个属性都有明确的“谁拥有、什么时候释放”的约定：
    - 数据库相关句柄（engine/checkpointer）在 ``close()`` 中统一释放；
    - 工具/Agent 是无状态或已绑定基础设施的对象，不需要额外释放；
    - reranker 启动时预热权重，可能为 None（``RERANKER_ENABLED=0``）。
    """

    settings: Settings                  # 全量运行时配置（见 infrastructure/settings.py）
    product_repo: MySqlProductRepository   # 商品仓储：查商品 / 扣库存 / 恢复库存
    order_repo: MySqlOrderRepository       # 订单仓储：建单 / 查单 / 幂等键查询
    search_usecase: CatalogSearchUseCase   # 检索用例：embedding→recall→rerank→filter
    place_order: PlaceOrderUseCase         # 下单用例：校验库存、创建订单并扣减库存
    query_order: QueryOrderUseCase         # 查单用例：按订单号查询订单快照
    cancel_order: CancelOrderUseCase       # 取消用例：取消订单并恢复库存
    tools: list                         # 全量工具（与 main_tools + trade_tools 并集一致，含偏好工具），
                                        # 供 smoke / 调试按名取用；Agent 绑定见 main_tools / trade_tools
    main_tools: list                    # P12-2 主 Agent 绑定工具：只读工具（product_search / category /
                                        # query_order / web_search）+ 偏好记忆写工具；不含 create_order / cancel_order
    trade_tools: list                   # P12-2 trade_agent 绑定工具：create_order / query_order / cancel_order
    agent: object                       # 编译好的 LangGraph 主图（CompiledStateGraph）
    engine: object                      # SQLAlchemy AsyncEngine；close() 释放连接池
    checkpointer: object                # LangGraph MySQL checkpointer 句柄；close() 关闭
    broker: EventBroker                 # SSE 事件总线；publish 给前端，同时写 trace
    semantic_cache: SemanticCache | DisabledSemanticCache  # 语义缓存：Redis 精确热路径 + Chroma 语义回退
    preference_store: MySqlPreferenceStore  # MySQL 长期偏好：buyer 的 like/dislike
    feedback_store: MySqlFeedbackStore  # P10 用户反馈：点赞/点踩 + 原因
    metrics_store: MySqlMetricsStore    # P10-5 会话成本/用量：每轮 /commerce/intents 写一行
    badcase_store: JsonlBadcaseWriter   # P10 点踩样本：data/badcases.jsonl
    task_store: MySqlTaskStateStore     # P8 任务状态：租约/generation/VERIFIED 证据
    category_kb: CategoryKnowledgeBase  # 品类知识 RAG：knowledge/*.md 分块入库
    reranker: object                    # BGE CrossEncoder 或 None；检索精排用
    redis_cache: RedisCache             # P9 Redis 可选热缓存/限流/幂等；disabled 时降级
    confirmation_manager: OrderConfirmationManager  # P10-2 下单确认握手：token → 等待/决议
    experiment_config: dict             # P10-5 当前 A/B 开关快照：Dashboard 展示用
    task_queue: object | None = None    # P11-4 多 worker 队列；QUEUE_MODE=direct 时为 None

    async def close(self) -> None:
        """释放数据库引擎、checkpointer、Redis 连接与事件背板。"""
        broker = getattr(self, "broker", None)
        if broker is not None:
            await broker.aclose()
        task_queue = getattr(self, "task_queue", None)
        if task_queue is not None:
            await task_queue.close()
        await self.redis_cache.close()
        await self.engine.dispose()
        await self.checkpointer.close()


async def build_container(
    settings: Settings,
    *,
    task_queue: object | None = None,
    backplane: object | None = None,
) -> Container:
    """按 settings 完成建表 / 灌种子 / 建索引 / 装配工具与主图。

    Args:
        settings: 运行时配置。
        task_queue: 可选外部注入的任务队列（测试用；缺省按 QUEUE_MODE 自动构造）。
        backplane: 可选外部注入的事件背板（测试用；缺省按 QUEUE_MODE 自动构造）。

    Returns:
        装配好的 ``Container``。
    """
    # ── 第 1 步：数据库。建表 + 三个仓储（商品/订单/P8 任务状态）。──
    engine = create_engine(settings.database_url)
    await bootstrap_schema(engine)

    product_repo = MySqlProductRepository(engine)
    order_repo = MySqlOrderRepository(engine)
    task_store = MySqlTaskStateStore(engine)
    # P10-5：会话成本 / 用量指标；每轮 /commerce/intents 结束写一行。
    metrics_store = MySqlMetricsStore(engine)

    # P11-1：订单号序列按“历史订单号最大值”初始化（只增不减，多进程重启也安全）。
    # 必须在服务对外可用之前完成：否则历史库里的 GBX- 号段可能与新号重复。
    counter_value = await seed_order_counter(engine)
    logger.info("订单号序列已就绪：%s（下一个 %s）", counter_value, format_order_id(counter_value + 1))

    # 空库才灌 60 条种子商品，避免每次启动重复覆盖库存。
    if await product_repo.count() == 0:
        await product_repo.insert_many(build_seed_products())

    # ── 第 2 步：检索基础设施。embedder 复用 Chroma 默认本地 ONNX 模型。──
    embedder = ChromaLocalEmbeddingClient()
    vector_index = ChromaProductIndex(settings)
    await bootstrap_product_index(product_repo, embedder, vector_index)

    # Reranker 对象轻量创建；启动时预热 1.1GB 权重，避免首次检索阻塞事件循环。
    reranker = BgeReranker(settings.reranker_model) if settings.reranker_enabled else None
    if reranker is not None:
        # 必须在构建工具/主图之前完成：服务对外可用时模型已就绪，首次 rerank 不再冷加载。
        await reranker.warmup()

    # ── 第 3 步：缓存 / trace / SSE / 长期记忆 / 品类 RAG。──
    # P9：Redis 可选基础设施；REDIS_URL 为空时构造 disabled 实例，不发起网络连接。
    redis_cache = RedisCache(settings.redis_url)
    # P9：语义缓存注入 Redis 热路径；disabled 时内部自动退回原 Chroma 路径。
    # P10-5：SEMANTIC_CACHE_ENABLED=0 时用空实现，连 Chroma 客户端都不初始化。
    if settings.semantic_cache_enabled:
        semantic_cache = SemanticCache(settings, embedder, redis_cache)
    else:
        semantic_cache = DisabledSemanticCache()
    # P6：每个 SSE 事件同时落一条本地 JSONL trace（data/traces/{session_id}.jsonl）。
    trace_logger = LocalTraceLogger(settings.data_dir)
    # P11-4：QUEUE_MODE=redis 才启用「队列 + 跨实例 SSE 广播」。
    # 默认 direct 时 backplane / task_queue 都是 None，broker 行为与 P11-3 完全一致，
    # 也不会因为 .env 里配了 REDIS_URL 就产生额外的 Pub/Sub 连接（本地开发零负担）。
    redis_url = getattr(settings, "redis_url", "") or ""
    queue_mode = (getattr(settings, "queue_mode", "direct") or "direct").strip().lower()
    if backplane is None and queue_mode == "redis" and redis_url:
        # 同一进程的 SSE 连接也要能收到其他 worker 实例发布的事件，所以背板常开。
        backplane = RedisEventBackplane.from_settings(settings)
    if task_queue is None and queue_mode == "redis":
        if redis_url:
            task_queue = RedisStreamTaskQueue.from_settings(settings)
        else:
            logger.warning("QUEUE_MODE=redis 但 REDIS_URL 为空：回落 direct（请求内直跑 Agent）")
    broker = EventBroker(trace_logger=trace_logger, backplane=backplane)

    preference_store = MySqlPreferenceStore(engine)
    # P10：用户反馈落 MySQL；点踩样本旁路追加 JSONL，供离线评测回流。
    feedback_store = MySqlFeedbackStore(engine)
    badcase_store = JsonlBadcaseWriter(settings.data_dir / "badcases.jsonl")
    category_kb = CategoryKnowledgeBase(settings, embedder)
    await category_kb.bootstrap(PROJECT_ROOT / "knowledge")

    # ── 第 4 步：用例层。把“仓储能力”组合成“业务动作”。──
    # preference_store 注入检索用例：商品卡 reasons 的"偏好记忆命中"必须基于真实偏好，
    # 由 usecase 在检索时按当前 buyer 读取，模型不需要也不应该自己编。
    search_usecase = CatalogSearchUseCase(
        product_repo=product_repo,
        embedder=embedder,
        vector_index=vector_index,
        reranker=reranker,
        tracer=trace_logger,
        preference_store=preference_store,
    )
    place_order = PlaceOrderUseCase(product_repo, order_repo)
    query_order = QueryOrderUseCase(order_repo)
    cancel_order = CancelOrderUseCase(product_repo, order_repo)

    # P10-2 修复：进程内下单确认握手。create_order 发布 order.preview 后真实挂起，
    # 等前端 POST /commerce/orders/confirm 决议；取消 / 超时绝不落库。
    confirmation_manager = OrderConfirmationManager()
    # 确认等待可能远长于普通工具超时：本工具单独放宽为 confirm_timeout 再留缓冲，
    # 保证 create_order 内部的 manager.wait 先按自己的 timeout 返回取消结果。
    order_confirm_wrapper_timeout = settings.order_confirm_timeout_seconds + ORDER_CONFIRM_TIMEOUT_BUFFER_SECONDS

    # ── 第 5 步：把用例包装成 LangChain StructuredTool，并接上 broker/task_store。──
    product_search = build_product_search_tool(search_usecase, broker=broker)
    order_tools = build_order_tools(
        place_order,
        query_order,
        cancel_order,
        broker=broker,
        task_store=task_store,
        task_id_prefix="order",
        redis_cache=redis_cache,
        confirmation_manager=confirmation_manager,
        settings=settings,
    )
    category_tool = build_category_insight_tool(category_kb, broker)
    preference_tools = build_preference_tools(preference_store, broker)

    # P7：有 TAVILY_API_KEY 才注册 WebSearch；缺省静默降级，不影响其他工具。
    tavily_api_key = getattr(settings, "tavily_api_key", "")
    web_search_tool = None
    if tavily_api_key:
        web_search_tool = build_web_search_tool(
            TavilySearchClient(tavily_api_key),
            broker=broker,
        )
    else:
        logger.info("web_search_tool 未注册（未配置 TAVILY_API_KEY）")

    # P4：LLM 网关并发闸门 + 业务工具统一超时/熔断包装。
    # throttle 限制“同时有几个 LLM 请求在飞”；breaker 按工具名分别记连续失败。
    throttle = GatewayThrottle(settings.llm_max_concurrency, tracer=trace_logger)
    breaker_registry = CircuitBreakerRegistry(settings)

    def _resilient(tool, timeout_seconds=None):
        """用统一熔断注册表给单个工具包一层超时 / 熔断。

        Args:
            tool: 原始 StructuredTool。
            timeout_seconds: 可选超时覆盖（下单确认工具需要等待买家）。

        Returns:
            包装后的 StructuredTool。
        """
        return build_resilient_tool(
            tool,
            settings,
            broker,
            registry=breaker_registry,
            tracer=trace_logger,
            timeout_seconds=timeout_seconds,
        )

    # 包装顺序很重要：先包装，再加入 all_tools；graph 只会看到“有韧性的工具”。
    product_search = _resilient(product_search)
    order_tools = [
        _resilient(
            tool,
            timeout_seconds=(
                order_confirm_wrapper_timeout if tool.name == "create_order" else None
            ),
        )
        for tool in order_tools
    ]
    category_tool = _resilient(category_tool)
    preference_tools = [_resilient(tool) for tool in preference_tools]
    if web_search_tool is not None:
        web_search_tool = _resilient(web_search_tool)

    # ── 第 6 步：按“哪个子 Agent 能用哪些工具”分配工具集。──
    # P12-2 关键约定：读写分离。create_order / cancel_order 是写工具，只出现在
    # trade_tools；main_agent 只拿只读兜底工具，避免主 Agent 绕过 planner 直接写库。
    # search_agent：商品检索 + 品类知识；trade_agent：只有订单工具。
    order_tools_by_name = {tool.name: tool for tool in order_tools}
    query_order_tool = order_tools_by_name["query_order"]
    read_only_tools = [product_search, category_tool, query_order_tool]
    if web_search_tool is not None:
        read_only_tools.append(web_search_tool)
    # 偏好记忆（remember / forget）是 main_agent 专属的用户记忆写工具，trade_agent
    # 不绑定；P12-2 只收紧订单写工具，故这里仍保留在 main_tools。
    main_tools = [*read_only_tools, *preference_tools]
    # all_tools 保持与 main_tools + trade_tools 的并集一致（顺序：只读→写→记忆）。
    all_tools = [*read_only_tools, *order_tools, *preference_tools]
    search_tools = [product_search, category_tool]
    if web_search_tool is not None:
        search_tools.append(web_search_tool)
    trade_tools = order_tools

    # ── 第 7 步：LLM。目前四个角色共用同一模型，便于本地调试与成本控制。──
    main_llm = build_chat_model(settings)
    planner_llm = main_llm    # 结构化输出：PlannerDecision
    search_llm = main_llm     # 搜索子 Agent
    trade_llm = main_llm      # 交易子 Agent

    # ── 第 8 步：LangGraph checkpointer + 主图。──
    # 多轮对话状态持久化到 MySQL；主图拿到 checkpointer.saver 后即可断点续聊。
    checkpointer = await build_checkpointer(settings.database_url)
    agent = build_main_graph(
        settings,
        main_llm,
        planner_llm,
        search_llm=search_llm,
        trade_llm=trade_llm,
        all_tools=all_tools,
        main_tools=main_tools,
        search_tools=search_tools,
        trade_tools=trade_tools,
        checkpointer=checkpointer.saver,
        broker=broker,
        preference_store=preference_store,
        preference_top_k=settings.preference_top_k,
        throttle=throttle,
        tracer=trace_logger,
        task_store=task_store,
    )

    # P10-5：把当前实验开关固化成快照，Dashboard 只读展示，不反向改配置。
    experiment_config = experiment_config_from_settings(settings)

    return Container(
        settings=settings,
        product_repo=product_repo,
        order_repo=order_repo,
        search_usecase=search_usecase,
        place_order=place_order,
        query_order=query_order,
        cancel_order=cancel_order,
        tools=all_tools,
        main_tools=main_tools,
        trade_tools=trade_tools,
        agent=agent,
        engine=engine,
        checkpointer=checkpointer,
        broker=broker,
        semantic_cache=semantic_cache,
        preference_store=preference_store,
        feedback_store=feedback_store,
        metrics_store=metrics_store,
        badcase_store=badcase_store,
        task_store=task_store,
        category_kb=category_kb,
        reranker=reranker,
        redis_cache=redis_cache,
        confirmation_manager=confirmation_manager,
        experiment_config=experiment_config,
        task_queue=task_queue,
    )
