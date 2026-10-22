/**
 * P10-2 下单确认与证据展示：纯函数核心。
 *
 * 本文件不依赖 React / DOM / fetch，只做「后端 SSE 事件 → 前端状态」的解析与推导，
 * 既被 `App.tsx` 复用，也被 `frontend/tests/order-preview.test.mts` 用 Node 原生
 * test runner 直接加载做单测（Node 24 原生剥离类型，无需 vitest / ts-node）。
 *
 * 设计约束（Task 2 修复后）：真实门控由后端 `OrderConfirmationManager` 承担，
 * 前端只消费两类事件：
 *   - order.preview: {"tool": "create_order", "token": ..., "preview": {items, shipping_address}}
 *     ← 收到它时后端已挂起等待；前端展示确认卡并 POST /commerce/orders/confirm。
 *   - tool.result:   {"tool": "create_order", "order": {..., status, verification_status}}
 *     ← 确认 / 取消后后端返回结果，前端展示结果卡与证据徽章。
 * 兼容旧事件 tool.invoke（后端直接执行路径 / 事件回放）作为预览兜底。
 */

/** 订单预览状态机的四个态。 */
export const PREVIEW_STATE = {
  IDLE: "idle",
  PENDING: "pending",
  CONFIRMED: "confirmed",
  CANCELLED: "cancelled",
} as const;

export type PreviewState = (typeof PREVIEW_STATE)[keyof typeof PREVIEW_STATE];

/** 订单状态 → 中文展示标签。 */
const ORDER_STATUS_LABELS: Record<string, string> = {
  CONFIRMED: "已确认",
  PENDING: "待确认",
  CANCELLED: "已取消",
  CANCELED: "已取消",
};

/** 证据徽章描述。 */
export interface VerificationBadge {
  tone: "green" | "amber";
  label: string;
  short: string;
  hint: string;
  queryable: boolean;
}

/** verification_status → 徽章样式 / 文案。 */
const VERIFICATION_BADGES: Record<string, VerificationBadge> = {
  VERIFIED: {
    tone: "green",
    label: "已验证",
    short: "VERIFIED",
    hint: "已用 query_order 反查核对，订单真实存在。",
    queryable: false,
  },
  EXECUTED: {
    tone: "amber",
    label: "结果待确认",
    short: "EXECUTED",
    hint: "下单请求已提交，尚未反查核对，可点「查询订单」确认。",
    queryable: true,
  },
  UNVERIFIED: {
    tone: "amber",
    label: "结果待确认",
    short: "UNVERIFIED",
    hint: "证据落库失败，结果待确认，可点「查询订单」确认。",
    queryable: true,
  },
  UNKNOWN: {
    tone: "amber",
    label: "结果待确认",
    short: "UNKNOWN",
    hint: "后端未返回验证状态，结果待确认，可点「查询订单」确认。",
    queryable: true,
  },
};

/** verification_status 缺省时的兜底徽章（UNKNOWN）。 */
const UNKNOWN_BADGE: VerificationBadge = {
  tone: "amber",
  label: "结果待确认",
  short: "UNKNOWN",
  hint: "后端未返回验证状态，结果待确认，可点「查询订单」确认。",
  queryable: true,
};

/** create_order 单行入参。 */
export interface OrderItemArgs {
  product_id: string;
  sku_id: string;
  quantity: number;
}

/** create_order 入参（tool.invoke 的 args）。 */
export interface CreateOrderArgs {
  items: OrderItemArgs[];
  shipping_address: Record<string, unknown>;
}

/** 预览 / 结果卡片里的行项目。 */
export interface PreviewLine {
  key: string;
  title: string;
  product_id: string;
  sku_id: string;
  quantity: number;
  unit_price_major?: number;
  line_total_major?: number;
}

/** 预览卡片 / 结果卡片共用的展示数据。 */
export interface OrderCard {
  /** invocation=旧 tool.invoke 兜底 / preview=order.preview 真实握手 / result=订单快照。 */
  source: "invocation" | "preview" | "result";
  order_id: string;
  status: string;
  status_label: string;
  items: PreviewLine[];
  item_count: number;
  address_text: string;
  subtotal_major?: number;
  total_major?: number;
  currency: string;
  amount_known: boolean;
  cancel_reason?: string;
  created_at?: string;
}

/** order.preview 事件的确认载荷：token + 预览（不含单价）。 */
export interface OrderPreviewPayload {
  token: string;
  preview: CreateOrderArgs;
  trace_id: string;
}

/** preview 状态机当前态。 */
export interface PreviewSnapshot {
  state: PreviewState;
  order_id: string;
  token?: string;
}

/** applyPreviewAction 产出的可执行动作（App.tsx 负责真正发请求）。 */
export type PreviewAction =
  | { type: "confirm_order"; token: string; endpoint: string }
  | { type: "cancel_order"; token: string; endpoint: string }
  | { type: "abandon_preview" }
  | { type: "query_order"; order_id: string; endpoint: string; label: string };

/**
 * 判断一个值是否为普通对象（排除 null / 数组）。
 *
 * @param value 待判断的值。
 * @returns 是否为可索引对象。
 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 读取非空字符串字段。
 *
 * @param record 对象。
 * @param key 字段名。
 * @returns 字符串值或 undefined。
 */
function readString(record: unknown, key: string): string | undefined {
  const value = isRecord(record) ? record[key] : undefined;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * 读取有限数字字段（兼容数字字符串）。
 *
 * @param record 对象。
 * @param key 字段名。
 * @returns 有限数字或 undefined。
 */
function readNumber(record: unknown, key: string): number | undefined {
  const value = isRecord(record) ? record[key] : undefined;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

/**
 * 解析 tool.invoke 事件：只认 create_order 且 args 完整时返回载荷，否则 null。
 *
 * @param event SSE tool.invoke 事件载荷。
 * @returns create_order 入参；不匹配返回 null。
 */
export function parseCreateOrderInvocation(event: unknown): CreateOrderArgs | null {
  if (!isRecord(event)) return null;
  if (readString(event, "tool") !== "create_order") return null;
  const args = event.args;
  if (!isRecord(args)) return null;
  const rawItems = Array.isArray(args.items) ? args.items : null;
  const rawAddress = isRecord(args.shipping_address) ? args.shipping_address : null;
  if (!rawItems || rawItems.length === 0 || !rawAddress) return null;

  const items: OrderItemArgs[] = rawItems.filter((item) => isRecord(item)).map((item) => ({
    product_id: readString(item, "product_id") ?? "",
    sku_id: readString(item, "sku_id") ?? "",
    quantity: readNumber(item, "quantity") ?? 1,
  }));

  const itemsValid = items.length > 0 && items.every((item) => item.product_id || item.sku_id);
  const addressValid = Boolean(
    readString(rawAddress, "recipient_name") ||
      readString(rawAddress, "address_line") ||
      readString(rawAddress, "city"),
  );
  if (!itemsValid || !addressValid) return null;

  return { items, shipping_address: { ...rawAddress } };
}

/**
 * 解析 order.preview 事件：返回 {token, preview, trace_id}，不匹配 / 残缺返回 null。
 *
 * 这是 Task 2 修复后的主路径：收到该事件时后端 create_order 已真实挂起等待，
 * 前端必须把 token 原样回传 POST /commerce/orders/confirm 才能放行 / 取消。
 *
 * @param event SSE order.preview 事件载荷。
 * @returns 确认载荷；不匹配 / 无 token / 预览残缺返回 null。
 */
export function parseOrderPreview(event: unknown): OrderPreviewPayload | null {
  if (!isRecord(event)) return null;
  if (readString(event, "tool") !== "create_order") return null;
  const token = readString(event, "token");
  if (!token) return null;
  const preview = isRecord(event.preview) ? event.preview : null;
  if (!preview) return null;
  // 复用 tool.invoke 的入参校验：把 preview 包成它认识的 event 形状。
  const args = parseCreateOrderInvocation({ tool: "create_order", args: preview });
  if (!args) return null;
  return { token, preview: args, trace_id: readString(event, "trace_id") ?? "" };
}

/**
 * 把 order.preview 载荷组装成预览卡片数据（source=preview，order_id 尚未生成）。
 *
 * @param envelope order.preview 事件载荷。
 * @returns 预览卡片数据；无法解析返回 null。
 */
export function buildPreviewFromOrderPreview(envelope: unknown): OrderCard | null {
  const parsed = parseOrderPreview(envelope);
  if (!parsed) return null;
  const card = buildPreviewFromInvocation(parsed.preview);
  if (!card) return null;
  return { ...card, source: "preview", order_id: "" };
}

/**
 * 解析 tool.result 事件：返回 {order} / {error}，与订单无关返回 null。
 *
 * @param event SSE tool.result 事件载荷。
 * @returns 订单结果或错误。
 */
export function parseOrderToolResult(
  event: unknown,
): { order?: Record<string, unknown>; error?: string } | null {
  if (!isRecord(event)) return null;
  const tool = readString(event, "tool");
  if (tool !== "create_order" && tool !== "query_order" && tool !== "cancel_order") return null;
  if (isRecord(event.order)) return { order: { ...event.order } };
  const error = readString(event, "error");
  if (error) return { error };
  return null;
}

/**
 * 地址对象 → 单行展示串。
 *
 * @param address 地址对象或字符串。
 * @returns 展示用地址文本。
 */
export function formatAddress(address: unknown): string {
  if (typeof address === "string") return address.trim();
  if (!isRecord(address)) return "";
  const parts = [
    address.recipient_name,
    address.phone,
    [address.country, address.state, address.city].filter(Boolean).join(" "),
    address.address_line,
    address.postal_code,
  ]
    .map((part) => (typeof part === "string" ? part.trim() : ""))
    .filter((part) => part.length > 0);
  return parts.join(" · ");
}

/**
 * 把 tool.invoke 的 create_order 入参组装成订单预览卡片数据。
 *
 * 入参里没有单价，因此小计 / 合计可能为 undefined —— 前端据此渲染
 * 「下单后由后端返回金额」的占位，绝不自行编造价格。
 *
 * @param args create_order 入参（items + shipping_address）。
 * @returns 预览卡片数据；无法解析返回 null。
 */
export function buildPreviewFromInvocation(args: unknown): OrderCard | null {
  if (!isRecord(args)) return null;
  const rawItems = Array.isArray(args.items) ? args.items : [];
  const rawAddress = isRecord(args.shipping_address) ? args.shipping_address : null;
  if (rawItems.length === 0 || !rawAddress) return null;

  let hasAllPrices = true;
  let subtotal = 0;
  const items: PreviewLine[] = rawItems.map((item, index) => {
    const record = isRecord(item) ? item : {};
    const quantity = readNumber(record, "quantity") ?? 1;
    const unitPrice = readNumber(record, "unit_price_major") ?? readNumber(record, "unit_price");
    if (unitPrice === undefined) {
      hasAllPrices = false;
    } else {
      subtotal += unitPrice * quantity;
    }
    return {
      key: `${readString(record, "product_id") ?? index}-${readString(record, "sku_id") ?? index}`,
      title: readString(record, "title") ?? readString(record, "product_id") ?? "未命名商品",
      product_id: readString(record, "product_id") ?? "",
      sku_id: readString(record, "sku_id") ?? "",
      quantity,
      unit_price_major: unitPrice,
      line_total_major: unitPrice === undefined ? undefined : unitPrice * quantity,
    };
  });

  return {
    source: "invocation",
    order_id: "",
    status: "PENDING",
    status_label: ORDER_STATUS_LABELS.PENDING,
    items,
    item_count: items.reduce((sum, item) => sum + item.quantity, 0),
    address_text: formatAddress(rawAddress),
    subtotal_major: hasAllPrices ? subtotal : undefined,
    total_major: hasAllPrices ? subtotal : undefined,
    currency: readString(rawAddress, "currency") ?? readString(args, "currency") ?? "CNY",
    amount_known: hasAllPrices,
  };
}

/**
 * 把 create_order / query_order / cancel_order 的结果快照组装成结果卡片数据。
 *
 * @param order 订单快照（Order.snapshot() 的结构）。
 * @returns 结果卡片数据；无法解析返回 null。
 */
export function buildResultCard(order: unknown): OrderCard | null {
  if (!isRecord(order)) return null;
  const orderId = readString(order, "order_id");
  const status = (readString(order, "status") ?? "").toUpperCase();
  if (!orderId && !status) return null;

  const rawLines = Array.isArray(order.lines) ? order.lines : [];
  let subtotal = 0;
  let known = rawLines.length > 0;
  const items: PreviewLine[] = rawLines.map((line, index) => {
    const record = isRecord(line) ? line : {};
    const quantity = readNumber(record, "quantity") ?? 1;
    const unitPrice = readNumber(record, "unit_price_major") ?? readNumber(record, "unit_price");
    if (unitPrice === undefined) {
      known = false;
    } else {
      subtotal += unitPrice * quantity;
    }
    return {
      key: `${readString(record, "product_id") ?? index}-${readString(record, "sku_id") ?? index}`,
      title: readString(record, "title") ?? readString(record, "product_id") ?? "未命名商品",
      product_id: readString(record, "product_id") ?? "",
      sku_id: readString(record, "sku_id") ?? "",
      quantity,
      unit_price_major: unitPrice,
      line_total_major: unitPrice === undefined ? undefined : unitPrice * quantity,
    };
  });

  const totalMajor =
    readNumber(order, "total_amount_major") ?? (known && items.length > 0 ? subtotal : undefined);
  return {
    source: "result",
    order_id: orderId ?? "",
    status: status || "UNKNOWN",
    status_label: ORDER_STATUS_LABELS[status] ?? status ?? "未知",
    items,
    item_count: items.reduce((sum, item) => sum + item.quantity, 0),
    address_text: formatAddress(order.shipping_address),
    subtotal_major: known && items.length > 0 ? subtotal : undefined,
    total_major: totalMajor,
    currency: readString(order, "currency") ?? "",
    cancel_reason: readString(order, "cancel_reason") ?? "",
    created_at: readString(order, "created_at") ?? "",
    amount_known: totalMajor !== undefined,
  };
}

/**
 * verification_status → 徽章描述（VERIFIED 绿 / 其余统一「结果待确认」）。
 *
 * @param verificationStatus 后端 create_order 返回的 verification_status。
 * @returns 徽章描述。
 */
export function verificationBadge(verificationStatus: unknown): VerificationBadge {
  const key = typeof verificationStatus === "string" ? verificationStatus.trim().toUpperCase() : "";
  const badge = VERIFICATION_BADGES[key];
  return badge ? { ...badge } : { ...UNKNOWN_BADGE };
}

/** 证据区字段。 */
export interface OrderEvidence {
  order_id: string;
  status: string;
  status_label: string;
  verification_status: string;
  badge: VerificationBadge;
}

/**
 * 从订单工具结果解析证据区所需字段（status / order_id / verification_status）。
 *
 * @param order 订单快照。
 * @returns 证据字段。
 */
export function extractEvidence(order: unknown): OrderEvidence {
  const record = isRecord(order) ? order : {};
  const status = (readString(record, "status") ?? "").toUpperCase();
  const verificationStatus =
    (readString(record, "verification_status") ?? "").toUpperCase() || "UNKNOWN";
  return {
    order_id: readString(record, "order_id") ?? "",
    status: status || "UNKNOWN",
    status_label: ORDER_STATUS_LABELS[status] ?? status ?? "未知",
    verification_status: verificationStatus,
    badge: verificationBadge(verificationStatus),
  };
}

/**
 * 状态机：根据用户操作推导下一状态与需要执行的动作。
 *
 * 纯函数，不触发任何网络请求；App.tsx 只负责执行返回的 action。
 *
 * @param preview 当前预览状态。
 * @param action 用户操作。
 * @param options 取消原因等附加参数。
 * @returns 下一状态与动作描述。
 */
export function applyPreviewAction(
  preview: PreviewSnapshot | null,
  action: "confirm" | "cancel" | null | undefined,
  options: { reason?: string } = {},
): { state: PreviewState; action: PreviewAction | null } {
  const state: PreviewState = preview?.state ?? PREVIEW_STATE.IDLE;
  const orderId = preview?.order_id ?? "";
  const token = preview?.token ?? "";

  if (state === PREVIEW_STATE.IDLE) {
    return { state, action: null };
  }
  if (state === PREVIEW_STATE.CONFIRMED) {
    return {
      state,
      action: orderId
        ? {
            type: "query_order",
            order_id: orderId,
            endpoint: `/api/commerce/orders/${orderId}`,
            label: "查询订单",
          }
        : null,
    };
  }
  if (state === PREVIEW_STATE.CANCELLED) {
    return { state, action: null };
  }

  // state === PENDING：已收到 order.preview，后端挂着等 token 决议。
  if (action === "confirm") {
    if (token) {
      // 真实握手：POST /commerce/orders/confirm 唤醒后端 create_order 继续下单。
      return {
        state: PREVIEW_STATE.CONFIRMED,
        action: { type: "confirm_order", token, endpoint: "/api/commerce/orders/confirm" },
      };
    }
    // 兜底：旧 tool.invoke 事件且后端已直接执行（事件回放 / 多轮）→ 只做查单。
    if (orderId) {
      return {
        state: PREVIEW_STATE.CONFIRMED,
        action: {
          type: "query_order",
          order_id: orderId,
          endpoint: `/api/commerce/orders/${orderId}`,
          label: "查询订单",
        },
      };
    }
    return { state: PREVIEW_STATE.CONFIRMED, action: null };
  }
  if (action === "cancel") {
    if (token) {
      // 真实取消：resolve token=cancel，后端返回 cancelled 且不会下单。
      return {
        state: PREVIEW_STATE.CANCELLED,
        action: { type: "cancel_order", token, endpoint: "/api/commerce/orders/confirm" },
      };
    }
    // 兜底（旧 tool.invoke 且无 token）：本地放弃预览，不触发任何下单请求。
    return { state: PREVIEW_STATE.CANCELLED, action: { type: "abandon_preview" } };
  }
  return { state, action: null };
}

/**
 * 读取事件 trace_id（用于把 invoke / result 关联到同一轮）。
 *
 * @param event 事件载荷。
 * @returns trace id；无则空串。
 */
export function eventTraceId(event: unknown): string {
  return readString(event, "trace_id") ?? "";
}

/**
 * 按 trace_id 在所有事件里找同一轮的订单结果（create_order 优先）。
 *
 * @param events 已收到的事件列表。
 * @param traceId 目标 trace id。
 * @returns 订单快照；找不到返回 null。
 */
export function findResultForTrace(
  events: Array<{ event: string; payload: unknown }>,
  traceId: string,
): Record<string, unknown> | null {
  if (!Array.isArray(events) || !traceId) return null;
  let fallback: Record<string, unknown> | null = null;
  for (const entry of events) {
    if (!isRecord(entry)) continue;
    if (readString(entry, "event") !== "tool.result") continue;
    const payload = entry.payload;
    if (eventTraceId(payload) !== traceId) continue;
    const parsed = parseOrderToolResult(payload);
    if (!parsed?.order) continue;
    if (readString(payload, "tool") === "create_order") return parsed.order;
    if (fallback === null) fallback = parsed.order;
  }
  return fallback;
}
