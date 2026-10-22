/**
 * P10-3 偏好 chips：随请求提交的检索口径提示（结构化）。
 * 只提交用户显式勾选的槽位，未勾选的字段不出现在请求体里。
 */
export interface PreferenceHint {
  budget_max_major?: number;
  budget_currency?: string;
  ship_to?: string;
  category?: string;
}

export interface IntentRequest {
  shopping_session_id: string;
  buyer_id: string;
  query: string;
  locale?: string;
  currency?: string;
  preference_hint?: PreferenceHint;
}

export async function submitIntent(req: IntentRequest): Promise<string> {
  const res = await fetch("/api/commerce/intents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  const body = await res.json();
  return body.reply ?? "";
}

export function openEvents(sessionId: string): EventSource {
  return new EventSource(`/api/commerce/events?shopping_session_id=${sessionId}`);
}

// ---------------------------------------------------------------------------
// P12-3 子 Agent 失败：稳定标记 + 纯函数检测
// ---------------------------------------------------------------------------
//
// 后端 `app/application/agents/subagents.py` 在所有失败路径上都把
// `SUBAGENT_UNAVAILABLE` 前缀写进 AIMessage；`final.result` 的 `reply` 是主
// Agent 转述后的文本，因此前端按这个稳定标记做机检（spec 第 5 节）。
// 常量与后端逐字一致，避免两处漂移。

/** 与后端 `subagents.SUBAGENT_UNAVAILABLE` 逐字对齐的失败标记。 */
export const SUBAGENT_UNAVAILABLE_MARKER = "[subagent unavailable]";

export interface SubagentFailure {
  /** 文本里是否出现失败标记。 */
  failed: boolean;
  /** 第一条含标记的行（trim 后）；没有标记时为 ""。 */
  detail: string;
}

/**
 * 检测回复 / AIMessage 文本里的子 Agent 失败标记。
 *
 * 纯函数、不读 window、不抛异常：`final.result` 的 reply 可能是 undefined 或
 * 任意 JSON 值，统一按“没有标记”处理。partial 结果仍按原文本交给调用方展示，
 * 本函数只负责给错误卡片提供 `detail`。
 *
 * @param text `final.result.reply` / AIMessage.content / `[subagent unavailable]` 日志文本。
 * @returns 是否失败 + 展示用明细行。
 */
export function detectSubagentFailure(text: unknown): SubagentFailure {
  if (typeof text !== "string" || !text.includes(SUBAGENT_UNAVAILABLE_MARKER)) {
    return { failed: false, detail: "" };
  }
  const line = text
    .split(/\r?\n/)
    .map((item) => item.trim())
    .find((item) => item.includes(SUBAGENT_UNAVAILABLE_MARKER));
  return { failed: true, detail: line && line.length > 0 ? line : SUBAGENT_UNAVAILABLE_MARKER };
}

/** `detectSubagentFailure` 的布尔简写，供条件渲染 / 纯函数测试使用。 */
export function hasSubagentUnavailable(text: unknown): boolean {
  return detectSubagentFailure(text).failed;
}

// ---------------------------------------------------------------------------
// P10-4 会话历史：后端 thread 查询优先，失败时由调用方回退 localStorage
// ---------------------------------------------------------------------------

/** `GET /commerce/sessions` 的单项。 */
export interface SessionSummary {
  session_id: string;
  /** trace 里 final.result 的数量（不代表 checkpointer 一定有历史）。 */
  turns: number;
  /** 秒或毫秒时间戳；前端统一换算。 */
  updated_at: number;
  has_history: boolean;
  /** 最近一条回复的截断预览，仅用于后端独有会话的标题兜底。 */
  preview?: string;
}

/** 后端 thread 历史里的一条消息。 */
export interface ThreadMessage {
  role: string;
  content: string;
}

export interface SessionHistoryResponse {
  session_id: string;
  /** checkpointer=LangGraph thread / trace=trace 文件兜底 / empty=都没有。 */
  source: "checkpointer" | "trace" | "empty" | string;
  messages: ThreadMessage[];
}

/**
 * 拉取后端会话列表（trace 目录扫描，只读且 fail-safe）。
 *
 * @returns 会话列表；请求失败时抛错，由调用方回退 localStorage。
 */
export async function fetchSessionList(): Promise<SessionSummary[]> {
  const res = await fetch("/api/commerce/sessions");
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  const list = Array.isArray(body?.sessions) ? (body.sessions as unknown[]) : [];
  return list
    .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    .map((item) => ({
      session_id: String(item.session_id ?? ""),
      turns: Number.isFinite(Number(item.turns)) ? Number(item.turns) : 0,
      updated_at: Number.isFinite(Number(item.updated_at)) ? Number(item.updated_at) : 0,
      has_history: Boolean(item.has_history),
      ...(typeof item.preview === "string" ? { preview: item.preview } : {}),
    }))
    .filter((item) => item.session_id.length > 0);
}

/**
 * 拉取某会话的后端 thread 历史（LangGraph checkpointer 优先，trace 兜底）。
 *
 * @param sessionId 会话 / thread id。
 * @returns 后端历史；请求失败时抛错，由调用方回退本地记录。
 */
export async function fetchSessionHistory(sessionId: string): Promise<SessionHistoryResponse> {
  const res = await fetch(`/api/commerce/sessions/${encodeURIComponent(sessionId)}/history`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return {
    session_id: String(body?.session_id ?? sessionId),
    source: String(body?.source ?? "empty"),
    messages: Array.isArray(body?.messages) ? (body.messages as ThreadMessage[]) : [],
  };
}

// ---------------------------------------------------------------------------
// P10-2 订单确认与证据展示
// ---------------------------------------------------------------------------

/** 订单工具 / REST 返回的订单快照（与后端 Order.snapshot() 对齐）。 */
export interface OrderSnapshot {
  order_id: string;
  buyer_id?: string;
  status: string;
  total_amount_major?: number;
  currency?: string;
  shipping_address?: string;
  lines?: Array<{
    product_id: string;
    sku_id: string;
    title: string;
    unit_price_major?: number;
    quantity: number;
  }>;
  created_at?: string;
  cancel_reason?: string;
  verification_status?: string;
  idempotent_replay?: boolean;
  [key: string]: unknown;
}

/**
 * 查询订单（后端 GET /commerce/orders/{order_id}）。
 *
 * @param orderId 订单号。
 * @returns 订单快照。
 */
export async function fetchOrder(orderId: string): Promise<OrderSnapshot> {
  const res = await fetch(`/api/commerce/orders/${encodeURIComponent(orderId)}`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body as OrderSnapshot;
}

export interface OrderConfirmResponse {
  token: string;
  /** confirmation manager 返回状态：confirmed / cancelled / timeout / unknown。 */
  status: string;
  ok: boolean;
}

/**
 * 下单确认握手（后端 POST /commerce/orders/confirm → OrderConfirmationManager.resolve）。
 *
 * 前端收到 `order.preview` 事件后点「确认下单 / 取消」调用本接口；后端 resolve
 * 会真实唤醒挂起的 create_order：confirm 才继续下单，cancel 直接返回取消结果。
 *
 * @param token order.preview 事件携带的确认 token。
 * @param action confirm=确认下单 / cancel=取消。
 * @returns 后端确认结果。
 */
export async function confirmOrder(
  token: string,
  action: "confirm" | "cancel",
): Promise<OrderConfirmResponse> {
  const res = await fetch("/api/commerce/orders/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, action }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body as OrderConfirmResponse;
}

/**
 * 取消订单（后端 POST /commerce/orders/{order_id}/cancel → cancel_order 工具）。
 *
 * @param orderId 订单号。
 * @param reason 取消原因。
 * @returns 取消后的订单快照。
 */
export async function cancelOrder(orderId: string, reason: string): Promise<OrderSnapshot> {
  const res = await fetch(`/api/commerce/orders/${encodeURIComponent(orderId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body as OrderSnapshot;
}

// ---------------------------------------------------------------------------
// P10-1 反馈闭环
// ---------------------------------------------------------------------------

/** 点踩原因枚举：code 与后端 domain/feedback 保持一致，label 仅用于展示。 */
export const FEEDBACK_REASONS = [
  { code: "product_irrelevant", label: "商品不相关" },
  { code: "price_mismatch", label: "价格不符合" },
  { code: "too_slow", label: "回答太慢" },
  { code: "wrong_info", label: "信息有误" },
  { code: "other", label: "其他" },
] as const;

export interface FeedbackRequest {
  session_id: string;
  score: 1 | -1;
  query?: string;
  trace_id?: string;
  reply_preview?: string;
  reason?: string;
  buyer_id?: string;
}

export interface FeedbackItem {
  id: number;
  session_id: string;
  trace_id: string;
  buyer_id: string;
  query: string;
  reply_preview: string;
  score: number;
  reason: string;
  reason_label: string;
  created_at: string;
}

export interface FeedbackResponse {
  id: number;
  status: string;
  reason_label: string;
}

export async function submitFeedback(req: FeedbackRequest): Promise<FeedbackResponse> {
  const res = await fetch("/api/commerce/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body as FeedbackResponse;
}

// ---------------------------------------------------------------------------
// P10-1 指标 Dashboard / P10-5 成本与 A/B 实验
// ---------------------------------------------------------------------------

/** P10-5 A/B 实验开关快照（后端 Container.experiment_config）。 */
export interface ExperimentConfig {
  semantic_cache_enabled: boolean;
  reranker_enabled: boolean;
  model_route_mode: string;
}

export interface DashboardMetrics {
  sessions: number;
  turns: number;
  avg_turns: number;
  completion_rate: number;
  likes: number;
  dislikes: number;
  reason_distribution: Record<string, number>;
  ttft_ms: number;
  ttft_samples: number;
  cache_hits: number;
  cache_hit_rate: number;
  total_tokens_estimate: number;
  cost_estimate: number;
  cost_currency: string;
  price_per_1k_tokens: number;
  order_success: number;
  trace_sessions: number;
  errors: number;
  llm_calls: number;
  generated_at: string;
  /** 当前 A/B 实验配置；旧后端可能没有，前端按「未知」展示。 */
  experiment?: ExperimentConfig;
}

/** `GET /commerce/metrics` 的聚合结果。 */
export interface SessionMetricsAggregate {
  sessions: number;
  rows: number;
  turns: number;
  llm_calls: number;
  total_tokens: number;
  cache_hits: number;
  cache_hit_rate: number;
  cost_estimate: number;
  cost_currency: string;
  avg_turns: number;
  avg_tokens_per_turn: number;
  avg_cost_per_turn: number;
  session_id: string | null;
  recent: Array<Record<string, unknown>>;
  experiment?: ExperimentConfig;
  generated_at: string;
}

export async function getDashboard(): Promise<DashboardMetrics> {
  const res = await fetch("/api/commerce/dashboard");
  if (!res.ok) {
    throw new Error(`加载指标失败：HTTP ${res.status}`);
  }
  return (await res.json()) as DashboardMetrics;
}

/**
 * 拉取会话成本 / 用量聚合（session_metrics 表）。
 *
 * @param sessionId 可选会话过滤。
 * @returns 聚合结果；请求失败时抛错。
 */
export async function getSessionMetrics(sessionId?: string): Promise<SessionMetricsAggregate> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const res = await fetch(`/api/commerce/metrics${query}`);
  if (!res.ok) {
    throw new Error(`加载成本指标失败：HTTP ${res.status}`);
  }
  return (await res.json()) as SessionMetricsAggregate;
}
