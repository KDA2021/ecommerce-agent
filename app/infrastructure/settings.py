# -*- coding: utf-8 -*-
"""全部配置的唯一定义处：从环境变量 / 项目根目录 .env 读取。

读取优先级：**显式环境变量 > .env 文件 > 代码里的默认值**。
例如本地调试不需要改代码，只需要编辑项目根目录的 `.env`（可参考 `.env.example`）。

注意：
- 本模块在 `import` 时就会 `load_dotenv(PROJECT_ROOT / ".env")`；
- 所以 `from app.infrastructure.settings import Settings` 之后，
  直接调用 `load_settings()` 即可拿到全部配置。

P15-3 配置分组
--------------
字段按领域收敛成 4 个只读分组，调用点可以用更清楚的路径读配置：

- :class:`LLMConfig` -> ``settings.llm.model`` / ``settings.llm.base_url`` / ``settings.llm.api_key``
- :class:`ResilienceConfig` -> ``settings.resilience.tool_timeout_seconds`` /
  ``settings.resilience.failure_threshold`` / ``settings.resilience.circuit_reset_seconds`` /
  ``settings.resilience.llm_max_retries``
- :class:`SubAgentConfig` -> ``settings.subagents.timeout`` / ``settings.subagents.max_retries`` /
  ``settings.subagents.lease``
- :class:`RedisConfig` -> ``settings.redis.url``

兼容策略：**旧扁平字段一个不删**（``settings.llm_model``、``settings.redis_url``、
``settings.tool_timeout_seconds``、``settings.subagent_timeout_seconds`` ...），
值与分组字段逐字段相等。保证途径：

1. 扁平字段仍是 :class:`Settings` 的真实 dataclass 字段（构造签名、``dataclasses.replace``
   与旧测试的 ``Settings(...)`` 调用完全不变），`load_settings()` 也照旧从环境变量读；
2. 分组是 ``__post_init__`` 里从扁平字段派生的冻结快照，两者同源、不可能漂移；
3. 分组值相等（``eq=True``），因此 ``settings.llm.model == settings.llm_model`` 恒成立。

注意：分组字段刻意不参与 ``__eq__`` / ``__hash__`` / ``__repr__``（``compare=False,
repr=False``），比较与哈希仍只看扁平字段，避免同一份配置出现两套影响 ``==`` 的来源。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：settings.py 位于 app/infrastructure/ 下，
# parent.parent.parent 依次是 infrastructure -> app -> 项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 在读取任何环境变量之前，先把项目根目录下的 .env 加载进进程环境。
# 注意：已有环境变量不会被 .env 覆盖（load_dotenv 默认 override=False）。
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class LLMConfig:
    """大模型接入配置（别名视图，值来自 :class:`Settings` 同名字段）。

    Attributes:
        model: 模型名（旧路径 ``Settings.llm_model``）。
        base_url: OpenAI 兼容接口地址（旧路径 ``Settings.llm_base_url``）。
        api_key: API Key（旧路径 ``Settings.llm_api_key``）。
    """

    model: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class ResilienceConfig:
    """工具韧性 + LLM 重试配置（别名视图，值来自 :class:`Settings` 同名字段）。

    Attributes:
        tool_timeout_seconds: 单个工具调用超时（旧 ``tool_timeout_seconds``）。
        failure_threshold: 连续失败多少次打开熔断器（旧 ``tool_failure_threshold``）。
        circuit_reset_seconds: 熔断冷却秒数（旧 ``tool_circuit_reset_seconds``）。
        llm_max_retries: LLM 可重试异常的最大重试次数（旧 ``llm_max_retries``）。
    """

    tool_timeout_seconds: int
    failure_threshold: int
    circuit_reset_seconds: float
    llm_max_retries: int


@dataclass(frozen=True)
class SubAgentConfig:
    """子 Agent 预算与租约配置（别名视图，值来自 :class:`Settings` 同名字段）。

    Attributes:
        timeout: 单个子 Agent 最长执行秒数（旧 ``subagent_timeout_seconds``）。
        max_retries: 子 Agent 明确失败时的最大重试次数（旧 ``subagent_max_retries``）。
        lease: 子任务租约秒数（旧 ``subagent_lease_seconds``）。
    """

    timeout: float
    max_retries: int
    lease: float


@dataclass(frozen=True)
class RedisConfig:
    """Redis 接入配置（别名视图，值来自 :class:`Settings.redis_url`）。

    Attributes:
        url: REDIS_URL；空串 = Redis 能力整体关闭。
    """

    url: str


@dataclass(frozen=True)
class Settings:
    """运行时配置（frozen dataclass）。

    所有字段都来自环境变量 / .env；dataclass 冻结后配置只读，
    避免运行过程中被某个模块悄悄修改。
    """

    # ============================
    # 1. 大模型（DeepSeek OpenAI 兼容接口）
    # ============================
    llm_base_url: str          # LLM 服务地址，默认 DeepSeek 官方 v1 接口
    llm_api_key: str           # LLM API Key；来自 .env 的 LLM_API_KEY
    llm_model: str             # 使用的模型名，默认 deepseek-v4-flash

    # ============================
    # 2. 数据库与本地数据目录
    # ============================
    database_url: str          # MySQL 连接串（SQLAlchemy async URL 格式）
    data_dir: Path             # 所有本地数据根目录：traces、eval-report、chroma 等
    chroma_dir: Path           # Chroma 向量库持久化目录（默认 data_dir/chroma）

    # ============================
    # 3. 对话上下文压缩
    # ============================
    context_trigger_tokens: int    # 消息累计超过多少 token 就触发压缩
    context_keep_recent: int       # 压缩后保留最近多少条消息，避免丢上下文

    # ============================
    # 4. 语义缓存
    # ============================
    semantic_cache_threshold: float      # embedding 余弦距离阈值（越小越严格）
    semantic_cache_bucket_limit: int     # 同一买家同一作用域最多缓存多少条

    # ============================
    # 5. 长期偏好记忆
    # ============================
    preference_top_k: int        # 从 MySQL 读取并注入对话的偏好条数上限

    # ============================
    # 6. 本地 BGE Reranker
    # ============================
    reranker_enabled: bool       # 是否启用精排；关闭后退化为向量召回分数
    reranker_model: str          # HuggingFace 模型名，例如 BAAI/bge-reranker-base

    # ============================
    # 7. LangGraph 主图步数预算
    # ============================
    max_agent_steps: int = 15               # 保留字段：主图步数预算（当前代码未强校验）

    # ============================
    # 8. 工具韧性与并发控制（P4）
    # ============================
    # 30s：容纳冷 CPU 精排（含 1.1GB 权重首次加载/预热），同时仍能熔断真正卡死的工具。
    tool_timeout_seconds: int = 30          # 单个业务工具调用的超时时间
    tool_failure_threshold: int = 3         # 连续失败多少次打开熔断器
    tool_circuit_reset_seconds: float = 60.0  # 熔断器冷却多少秒后进入半开试探

    # ============================
    # 9. LLM 重试与并发闸门（P4）
    # ============================
    llm_max_retries: int = 2                # LLM 可重试异常的最大重试次数
    llm_retry_base_seconds: float = 2.0     # 指数退避的基数（第一次退避 2s）
    llm_max_concurrency: int = 2            # 全局 LLM 并发上限（GatewayThrottle 信号量）

    # ============================
    # 10. 子 Agent 预算与租约（P8）
    # ============================
    subagent_timeout_seconds: float = 120.0 # 单个子 Agent 的最长执行时间（P8）
    subagent_max_retries: int = 2           # 子 Agent 明确失败时的最大重试次数（P8）
    subagent_lease_seconds: float = 180.0   # 子任务租约时长；过期才允许接管（P8）

    # ============================
    # 10.1 trade 串行链预算（P12-1）
    # ============================
    # 一次请求内 trade_agent 串行链最多执行多少个子任务；超出后停止链，由主 Agent
    # 汇总已完成部分。来源 TRADE_CHAIN_MAX_STEPS；有默认值，兼容旧构造方式。
    trade_chain_max_steps: int = 5

    # ============================
    # 10. 第三方工具
    # ============================
    tavily_api_key: str = ""    # Tavily 搜索 Key；为空时不注册 web_search 工具

    # ============================
    # 11. Redis（P9，可选）
    # ============================
    redis_url: str = ""         # REDIS_URL；为空 = Redis 能力整体关闭（缓存/限流/幂等）

    # ============================
    # 12. 滑动窗口限流（P9-3，可选）
    # ============================
    rate_limit_per_minute: int = 30     # 每分钟软阈值：超过只回 Retry-After 提示，不拒绝
    rate_limit_burst: int = 60          # 突发硬上限：窗口内超过该值返回 429
    rate_limit_scope: str = "buyer"     # 限流维度：buyer（默认）或 ip

    # ============================
    # 13. 下单确认握手（P10-2 修复）
    # ============================
    # create_order 发布 order.preview 后真实挂起等待买家确认的最长秒数；
    # 超时按取消处理、绝不落库。来源：ORDER_CONFIRM_TIMEOUT_SECONDS。
    order_confirm_timeout_seconds: float = 120.0

    # ============================
    # 14. A/B 实验开关（P10-5）
    # ============================
    # 语义缓存开关：关闭后 /commerce/intents 跳过 lookup / remember，直接跑 Agent。
    # 字段有默认值，保持 Settings 对旧测试构造方式（只传既有字段）兼容。
    semantic_cache_enabled: bool = True
    # 模型路由模式：llm=planner LLM 派发子 Agent（默认）；direct=跳过 planner，
    # 由主 Agent 自己用 function calling 决策工具，作为成本 / 质量的 A/B 对照组。
    model_route_mode: str = "llm"

    # ============================
    # 15. API Key 鉴权（P11-3，可选）
    # ============================
    # 来源 API_KEY；空字符串 = 不鉴权，保持本地开发 / 既有测试的向后兼容。
    # 配置后 /commerce/* 需要 X-API-Key 请求头或 ?api_key= 查询参数；
    # /health 与 /docs 始终开放。
    api_key: str = ""
    buyer_api_keys_json: str = field(default="", repr=False)
    auth_cookie_secure: bool = False

    # ============================
    # 16. 多 worker 队列与广播（P11-4，可选）
    # ============================
    # 来源 QUEUE_MODE；direct（默认）= 请求内直接跑 Agent，行为与 P11-3 完全一致；
    # redis = /commerce/intents 只入队并立刻返回 task_id，由 scripts/agent_worker.py 消费。
    queue_mode: str = "direct"
    # Redis Stream key / 消费者组名（QUEUE_MODE=redis 时生效）。
    queue_stream_key: str = "ecommerce:intents"
    queue_group: str = "ecommerce-workers"

    # ============================
    # 17. 派生分组快照（P15-3，只读别名）
    # ============================
    # 由 __post_init__ 从上面的扁平字段派生：init=False 不进构造签名，
    # compare/repr=False 不参与 == / hash / repr，保证“一份配置一个身份”。
    _llm: LLMConfig = field(init=False, compare=False, repr=False)
    _resilience: ResilienceConfig = field(init=False, compare=False, repr=False)
    _subagents: SubAgentConfig = field(init=False, compare=False, repr=False)
    _redis: RedisConfig = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        """由扁平字段派生分组快照；frozen 实例必须用 ``object.__setattr__``。"""
        object.__setattr__(
            self,
            "_llm",
            LLMConfig(
                model=self.llm_model,
                base_url=self.llm_base_url,
                api_key=self.llm_api_key,
            ),
        )
        object.__setattr__(
            self,
            "_resilience",
            ResilienceConfig(
                tool_timeout_seconds=self.tool_timeout_seconds,
                failure_threshold=self.tool_failure_threshold,
                circuit_reset_seconds=self.tool_circuit_reset_seconds,
                llm_max_retries=self.llm_max_retries,
            ),
        )
        object.__setattr__(
            self,
            "_subagents",
            SubAgentConfig(
                timeout=self.subagent_timeout_seconds,
                max_retries=self.subagent_max_retries,
                lease=self.subagent_lease_seconds,
            ),
        )
        object.__setattr__(self, "_redis", RedisConfig(url=self.redis_url))

    @property
    def llm(self) -> LLMConfig:
        """LLM 分组：``settings.llm.model`` 等价于旧 ``settings.llm_model``。"""
        return self._llm

    @property
    def resilience(self) -> ResilienceConfig:
        """工具韧性 / LLM 重试分组（旧扁平字段继续可用）。"""
        return self._resilience

    @property
    def subagents(self) -> SubAgentConfig:
        """子 Agent 预算分组（旧扁平字段继续可用）。"""
        return self._subagents

    @property
    def redis(self) -> RedisConfig:
        """Redis 分组：``settings.redis.url`` 等价于旧 ``settings.redis_url``。"""
        return self._redis


def load_settings() -> Settings:
    """从环境变量读取并构造 Settings，同时确保 data_dir 存在。

    为什么统一在这里读：
    - 所有默认值只写一处，改配置不用满项目 grep；
    - 类型转换集中处理（int/float/bool），调用方拿到的就是正确类型。

    Returns:
        Settings: 冻结的运行时配置对象。
    """
    # data_dir 是其他目录的父目录，先确保存在；否则 trace/Chroma 首次写会失败。
    data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        # --- LLM ---
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),

        # --- 数据库与目录 ---
        database_url=os.getenv(
            "DATABASE_URL",
            # 本地开发默认连 Docker 里的 ecommerce-mysql 容器（宿主机 3307 端口）。
            "mysql+asyncmy://ecommerce:ecommerce@localhost:3307/ecommerce?charset=utf8mb4",
        ),
        data_dir=data_dir,
        chroma_dir=Path(os.getenv("CHROMA_DIR", str(data_dir / "chroma"))),

        # --- 上下文压缩 ---
        # 6000 token 是经验阈值：过早压缩浪费 LLM 调用，过晚压缩可能超出模型窗口。
        context_trigger_tokens=int(os.getenv("CONTEXT_TRIGGER_TOKENS", "6000")),
        # 压缩后保留最近 8 条，保证“你刚才问过什么”这类指代仍可解析。
        context_keep_recent=int(os.getenv("CONTEXT_KEEP_RECENT", "8")),

        # --- 语义缓存 ---
        # 0.05 是余弦距离阈值（不是相似度）；Chroma 返回 distance，越小越相似。
        semantic_cache_threshold=float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.05")),
        # 每买家每作用域最多 30 条缓存，超过会淘汰最旧的，防止缓存无限增长。
        semantic_cache_bucket_limit=int(os.getenv("SEMANTIC_CACHE_BUCKET_LIMIT", "30")),
        # P10-5 A/B 开关：0/false 关闭语义缓存，请求直接进入 Agent 路径。
        semantic_cache_enabled=os.getenv("SEMANTIC_CACHE_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),

        # --- 偏好记忆 ---
        preference_top_k=int(os.getenv("PREFERENCE_TOP_K", "5")),

        # --- Reranker ---
        # RERANKER_ENABLED 支持 0/1/true/false；除 0/false 外都视为启用。
        reranker_enabled=os.getenv("RERANKER_ENABLED", "1") not in ("0", "false", "False"),
        reranker_model=os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base"),

        # --- A/B 实验：模型路由模式（P10-5）---
        # llm=planner LLM 派发子 Agent；direct=主 Agent 自己决策（跳过 planner LLM）。
        model_route_mode=os.getenv("MODEL_ROUTE_MODE", "llm").strip().lower() or "llm",

        # --- 主图与子 Agent 预算（P8）---
        max_agent_steps=int(os.getenv("MAX_AGENT_STEPS", "15")),
        subagent_timeout_seconds=float(os.getenv("SUBAGENT_TIMEOUT_SECONDS", "120")),
        subagent_max_retries=int(os.getenv("SUBAGENT_MAX_RETRIES", "2")),
        subagent_lease_seconds=float(os.getenv("SUBAGENT_LEASE_SECONDS", "180")),

        # --- trade 串行链预算（P12-1）---
        trade_chain_max_steps=int(os.getenv("TRADE_CHAIN_MAX_STEPS", "5")),

        # --- 工具韧性（P4）---
        # 默认 30s：冷 CPU 精排可能占用十几秒；15s 会让并行检索集体超时并误开熔断。
        tool_timeout_seconds=int(os.getenv("TOOL_TIMEOUT_SECONDS", "30")),
        tool_failure_threshold=int(os.getenv("TOOL_FAILURE_THRESHOLD", "3")),
        tool_circuit_reset_seconds=float(os.getenv("TOOL_CIRCUIT_RESET_SECONDS", "60.0")),

        # --- LLM 重试与并发（P4）---
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        llm_retry_base_seconds=float(os.getenv("LLM_RETRY_BASE_SECONDS", "2.0")),
        llm_max_concurrency=int(os.getenv("LLM_MAX_CONCURRENCY", "2")),

        # --- WebSearch（P7）---
        # 空字符串表示未配置；bootstrap 据此跳过 web_search_tool 注册。
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),

        # --- Redis（P9，可选）---
        # 空字符串 = Redis 能力整体关闭，行为与 P8 一致；故障时各调用点 fail-safe。
        redis_url=os.getenv("REDIS_URL", ""),

        # --- 滑动窗口限流（P9-3）---
        # per_minute 是软阈值（仅提示），burst 是硬上限（超了 429）；scope 支持 buyer / ip。
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
        rate_limit_burst=int(os.getenv("RATE_LIMIT_BURST", "60")),
        rate_limit_scope=os.getenv("RATE_LIMIT_SCOPE", "buyer"),

        # --- 下单确认握手（P10-2 修复）---
        # 120s 覆盖买家从容核对商品 / 地址的时间；超时按取消处理，绝不落库。
        order_confirm_timeout_seconds=float(
            os.getenv("ORDER_CONFIRM_TIMEOUT_SECONDS", "120"),
        ),

        # --- API Key 鉴权（P11-3，可选）---
        # 空字符串 = 不鉴权；配置后 /commerce/* 校验 X-API-Key / ?api_key=。
        api_key=os.getenv("API_KEY", ""),
        buyer_api_keys_json=os.getenv("BUYER_API_KEYS_JSON", ""),
        auth_cookie_secure=os.getenv("AUTH_COOKIE_SECURE", "0").lower() in ("1", "true", "yes"),

        # --- 多 worker 队列与广播（P11-4，可选）---
        # direct（默认）= 请求内直接跑 Agent；redis = 入队 + worker 消费。
        # 非法取值统一回落 direct，避免部署时拼错环境变量导致请求无人消费。
        queue_mode=_normalize_queue_mode(os.getenv("QUEUE_MODE", "direct")),
        queue_stream_key=os.getenv("QUEUE_STREAM_KEY", "ecommerce:intents"),
        queue_group=os.getenv("QUEUE_GROUP", "ecommerce-workers"),
    )


def _normalize_queue_mode(raw: str) -> str:
    """把 QUEUE_MODE 归一化为 direct / redis；未知取值回落 direct。

    Args:
        raw: 环境变量原始值。

    Returns:
        ``"redis"`` 或 ``"direct"``。
    """
    value = (raw or "").strip().lower()
    return "redis" if value == "redis" else "direct"
