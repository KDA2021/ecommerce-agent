/**
 * P10-4 中英文 i18n 字典 + 空状态建议问题。
 *
 * 设计约束：
 *   - 纯数据 + 纯函数，不 import React / 不读 window，方便 Node 原生 test runner 单测；
 *   - 所有 key 在 zh / en 两份字典里必须齐全（`missingKeys()` 可自检）；
 *   - `t()` 支持 `{name}` 占位符；未知 key 依次回退 zh → 原 key，绝不抛异常。
 */

export type Lang = "zh" | "en";

export const LANGUAGES = ["zh", "en"] as const;
export const DEFAULT_LANG: Lang = "zh";

/** localStorage key：语言选择。 */
export const LANG_STORAGE_KEY = "ecommerce-agent.lang";

type Dict = Record<string, string>;

const ZH: Dict = {
  "app.subtitle": "React + Vite 前端：REST 提交意图，SSE 接收事件。",
  "nav.dashboard": "查看产品指标 Dashboard →",
  "lang.label": "语言",
  "lang.toggle": "English",

  "session.buyerId": "买家 ID",
  "session.sessionId": "会话 ID",

  "history.title": "会话历史",
  "history.new": "新会话",
  "history.empty": "暂无历史会话，发送第一条消息后会记录在这里。",
  "history.untitled": "未命名会话",
  "history.turns": "{count} 轮",
  "history.delete": "删除会话",
  "history.current": "当前",
  "history.fromBackend": "后端",
  "history.fromLocal": "本地",
  "history.sync": "同步后端历史",
  "history.syncing": "同步中…",
  "history.syncFailed": "后端 thread 不可用，已使用本地记录。",

  "chat.title": "对话",
  "chat.empty": "还没有消息，试试这些建议问题：",
  "chat.inputPlaceholder": "例如：帮我找旅行三件套",
  "chat.send": "发送",
  "chat.sending": "发送中…",
  "chat.retry": "重试",
  "role.buyer": "买家",

  "suggestion.title": "建议问题",

  "timeline.title": "事件时间线",
  "timeline.waiting": "等待 SSE 事件…",
  "timeline.searching": "正在检索商品…",
  "skeleton.loading": "加载商品卡片…",

  "error.request": "请求失败",
  "error.sse": "SSE 连接中断",
  "error.retry": "重试",
  "error.retrySse": "重新连接",
  "error.subagent": "子任务不可用",
  "error.subagentHint": "本轮有子任务失败，结果可能不完整；已拿到的 partial 结果仍在上方展示。重试会复用当前会话，后端幂等保证不会重复下单。",
  "error.retrySubagent": "重试本轮",

  "preference.title": "检索偏好",
  "preference.expand": "设置偏好",
  "preference.collapse": "收起偏好",
  "preference.budget": "预算",
  "preference.country": "收货国家",
  "preference.category": "品类",
  "preference.any": "不限",
  "preference.within": "{amount} {currency} 内",

  "product.reasonsEmpty": "无匹配理由",
  "product.origin": "产地",
  "product.shipsTo": "可发国家",
  "product.reason.budget_match": "预算命中",
  "product.reason.ship_to_match": "可发往收货国",
  "product.reason.preference_match": "命中你的偏好",

  "compare.title": "前 3 名对比",
  "compare.product": "商品",
  "compare.price": "价格",
  "compare.landed": "到手价",
  "compare.weight": "重量",
  "compare.countries": "可发国家",
  "compare.highlights": "亮点",

  "order.previewTitle": "下单确认",
  "order.pendingHint": "请核对商品、SKU、数量与收货地址后选择「确认下单」或「取消」。",
  "order.confirmedHint": "已确认，正在等待后端下单结果；可点「查询订单」核对。",
  "order.cancelledHint": "已取消，不会创建订单。",
  "order.expiredHint": "确认已过期（可能已超时），请重新发起下单。",
  "order.confirm": "确认下单",
  "order.cancel": "取消",
  "order.query": "查询订单",
  "order.cardPrefix": "订单",
  "order.pendingId": "（待生成）",
  "order.status": "状态",
  "order.itemCount": "共 {count} 件",
  "order.subtotal": "小计",
  "order.total": "合计",
  "order.address": "收货地址",
  "order.cancelReason": "取消原因",
  "order.unknownId": "未知订单号",
  "order.confirmingBadge": "已确认，等待下单结果",
  "order.pendingBadge": "下单前请确认",
  "order.amountPending": "下单后由后端返回",
  "order.verifiedBadge": "已验证",
  "order.unverifiedBadge": "结果待确认",
  "order.verifiedHint": "已用 query_order 反查核对，订单真实存在。",
  "order.unverifiedHint": "结果待确认，可点「查询订单」核对是否真实落库。",
  "order.status.pending": "待确认",
  "order.status.confirmed": "已确认",
  "order.status.cancelled": "已取消",
  "order.status.unknown": "未知",
  "order.lookupTitle": "订单证据查询",
  "order.lookupHint": "根据订单号重新查询，用于核对 UNKNOWN「结果待确认」的订单是否真实落库。",
  "order.lookupPlaceholder": "订单号，如 GBX-000001",

  "feedback.thanksLike": "感谢你的反馈（已点赞）",
  "feedback.thanksDislike": "感谢你的反馈（已点踩：{reason}）",
  "feedback.like": "点赞",
  "feedback.dislike": "点踩",
  "feedback.failed": "提交失败，请重试",
  "feedback.reason.product_irrelevant": "商品不相关",
  "feedback.reason.price_mismatch": "价格不符合",
  "feedback.reason.too_slow": "回答太慢",
  "feedback.reason.wrong_info": "信息有误",
  "feedback.reason.other": "其他",

  "dashboard.title": "产品指标 Dashboard",
  "dashboard.back": "← 返回对话",
  "dashboard.refresh": "刷新",
  "dashboard.refreshing": "刷新中…",
  "dashboard.source": "数据来源：trace（TTFT / 轮数 / 缓存命中）、MySQL 反馈与订单；成本为 token 估算值。",
  "dashboard.experiment": "A/B 实验配置",
  "dashboard.experimentHint": "启动时由环境变量读取，改配置需重启服务。",
  "dashboard.semanticCache": "语义缓存",
  "dashboard.reranker": "Reranker 精排",
  "dashboard.modelRoute": "模型路由",
  "dashboard.on": "开启",
  "dashboard.off": "关闭",
  "dashboard.experimentLoadFailed": "实验配置加载失败",
  "dashboard.sessions": "会话数",
  "dashboard.sessionsHint": "trace 会话 {count}",
  "dashboard.avgTurns": "平均轮数",
  "dashboard.avgTurnsHint": "总轮数 {count}",
  "dashboard.completion": "完成率",
  "dashboard.completionHint": "有 final.result 的会话占比",
  "dashboard.votes": "点赞 / 点踩",
  "dashboard.ttftHint": "{count} 个采样",
  "dashboard.cache": "缓存命中",
  "dashboard.cacheHint": "命中率 {rate}",
  "dashboard.orderSuccess": "下单成功",
  "dashboard.orderSuccessHint": "status = CONFIRMED",
  "dashboard.cost": "成本估计",
  "dashboard.costHint": "≈{tokens} tokens @ {price}/1K",
  "dashboard.llmCalls": "LLM 调用 span",
  "dashboard.errorsHint": "错误事件 {count}",
  "dashboard.reasons": "点踩原因分布（共 {count} 条）",
  "dashboard.noReasons": "暂无点踩反馈。",
};

const EN: Dict = {
  "app.subtitle": "React + Vite frontend: intents over REST, events over SSE.",
  "nav.dashboard": "View product metrics →",
  "lang.label": "Language",
  "lang.toggle": "中文",

  "session.buyerId": "Buyer ID",
  "session.sessionId": "Session ID",

  "history.title": "Session history",
  "history.new": "New chat",
  "history.empty": "No history yet. Your first message will be saved here.",
  "history.untitled": "Untitled chat",
  "history.turns": "{count} turns",
  "history.delete": "Delete session",
  "history.current": "Current",
  "history.fromBackend": "server",
  "history.fromLocal": "local",
  "history.sync": "Sync server history",
  "history.syncing": "Syncing…",
  "history.syncFailed": "Server thread unavailable; using the local record.",

  "chat.title": "Chat",
  "chat.empty": "No messages yet. Try one of these suggested questions:",
  "chat.inputPlaceholder": "e.g. Find a travel kit",
  "chat.send": "Send",
  "chat.sending": "Sending…",
  "chat.retry": "Retry",
  "role.buyer": "Buyer",

  "suggestion.title": "Suggested questions",

  "timeline.title": "Event timeline",
  "timeline.waiting": "Waiting for SSE events…",
  "timeline.searching": "Searching products…",
  "skeleton.loading": "Loading product cards…",

  "error.request": "Request failed",
  "error.sse": "SSE connection lost",
  "error.retry": "Retry",
  "error.retrySse": "Reconnect",
  "error.subagent": "Sub-agent unavailable",
  "error.subagentHint": "A sub-task failed this turn, so the result may be incomplete; partial results are still shown above. Retrying reuses the current session and backend idempotency prevents duplicate orders.",
  "error.retrySubagent": "Retry this turn",

  "preference.title": "Search preferences",
  "preference.expand": "Set preferences",
  "preference.collapse": "Hide preferences",
  "preference.budget": "Budget",
  "preference.country": "Ship to",
  "preference.category": "Category",
  "preference.any": "Any",
  "preference.within": "under {amount} {currency}",

  "product.reasonsEmpty": "No matched reasons",
  "product.origin": "Origin",
  "product.shipsTo": "Ships to",
  "product.reason.budget_match": "Within budget",
  "product.reason.ship_to_match": "Ships to your country",
  "product.reason.preference_match": "Matches your preference",

  "compare.title": "Top 3 comparison",
  "compare.product": "Product",
  "compare.price": "Price",
  "compare.landed": "Landed",
  "compare.weight": "Weight",
  "compare.countries": "Ships to",
  "compare.highlights": "Highlights",

  "order.previewTitle": "Order confirmation",
  "order.pendingHint": "Review the items, SKU, quantity and address, then confirm or cancel.",
  "order.confirmedHint": "Confirmed. Waiting for the order result; use “Query order” to verify.",
  "order.cancelledHint": "Cancelled. No order will be created.",
  "order.expiredHint": "This confirmation expired (possibly timed out). Please start a new order.",
  "order.confirm": "Confirm order",
  "order.cancel": "Cancel",
  "order.query": "Query order",
  "order.cardPrefix": "Order",
  "order.pendingId": "(pending)",
  "order.status": "Status",
  "order.itemCount": "{count} items",
  "order.subtotal": "Subtotal",
  "order.total": "Total",
  "order.address": "Shipping address",
  "order.cancelReason": "Cancel reason",
  "order.unknownId": "unknown id",
  "order.confirmingBadge": "Confirmed, awaiting result",
  "order.pendingBadge": "Confirm before ordering",
  "order.amountPending": "Returned after ordering",
  "order.verifiedBadge": "Verified",
  "order.unverifiedBadge": "Pending verification",
  "order.verifiedHint": "Verified with query_order: the order really exists.",
  "order.unverifiedHint": "Pending verification; use “Query order” to check whether it landed.",
  "order.status.pending": "Pending",
  "order.status.confirmed": "Confirmed",
  "order.status.cancelled": "Cancelled",
  "order.status.unknown": "Unknown",
  "order.lookupTitle": "Order evidence lookup",
  "order.lookupHint": "Query by order id to verify whether an UNKNOWN order really landed.",
  "order.lookupPlaceholder": "Order id, e.g. GBX-000001",

  "feedback.thanksLike": "Thanks for your feedback (liked)",
  "feedback.thanksDislike": "Thanks for your feedback (disliked: {reason})",
  "feedback.like": "Like",
  "feedback.dislike": "Dislike",
  "feedback.failed": "Submit failed, please retry",
  "feedback.reason.product_irrelevant": "Irrelevant products",
  "feedback.reason.price_mismatch": "Price mismatch",
  "feedback.reason.too_slow": "Too slow",
  "feedback.reason.wrong_info": "Wrong information",
  "feedback.reason.other": "Other",

  "dashboard.title": "Product metrics",
  "dashboard.back": "← Back to chat",
  "dashboard.refresh": "Refresh",
  "dashboard.refreshing": "Refreshing…",
  "dashboard.source": "Sources: traces (TTFT / turns / cache hits) and MySQL feedback & orders; cost is token-estimated.",
  "dashboard.experiment": "A/B experiment config",
  "dashboard.experimentHint": "Read from env vars at startup; restart to apply changes.",
  "dashboard.semanticCache": "Semantic cache",
  "dashboard.reranker": "Reranker",
  "dashboard.modelRoute": "Model route",
  "dashboard.on": "on",
  "dashboard.off": "off",
  "dashboard.experimentLoadFailed": "Failed to load experiment config",
  "dashboard.sessions": "Sessions",
  "dashboard.sessionsHint": "trace sessions {count}",
  "dashboard.avgTurns": "Avg turns",
  "dashboard.avgTurnsHint": "total turns {count}",
  "dashboard.completion": "Completion rate",
  "dashboard.completionHint": "sessions with final.result",
  "dashboard.votes": "Likes / dislikes",
  "dashboard.ttftHint": "{count} samples",
  "dashboard.cache": "Cache hits",
  "dashboard.cacheHint": "hit rate {rate}",
  "dashboard.orderSuccess": "Orders placed",
  "dashboard.orderSuccessHint": "status = CONFIRMED",
  "dashboard.cost": "Estimated cost",
  "dashboard.costHint": "≈{tokens} tokens @ {price}/1K",
  "dashboard.llmCalls": "LLM call spans",
  "dashboard.errorsHint": "error events {count}",
  "dashboard.reasons": "Dislike reasons ({count})",
  "dashboard.noReasons": "No dislike feedback yet.",
};

/** 中英文词典（同 key 集）。 */
export const DICT: Record<Lang, Dict> = { zh: ZH, en: EN };

/** 空状态建议问题：每种语言 5 条（纯文案，点击后直接作为 query 提交）。 */
export const SUGGESTED_QUESTIONS: Record<Lang, readonly string[]> = {
  zh: [
    "帮我找 200 元内的旅行三件套",
    "有适合送朋友的数码配件吗？",
    "推荐一款可发往美国的轻量收纳包",
    "帮我对比一下前两个商品的重量和到手价",
    "帮我下单，寄到上海市浦东新区",
  ],
  en: [
    "Find a travel kit under 200 CNY",
    "Any gadgets that make a good gift?",
    "Recommend a lightweight organizer that ships to the US",
    "Compare the weight and landed price of the top two",
    "Place an order shipped to Shanghai",
  ],
};

/** 反馈点踩原因 code → 文案（与后端 REASON_LABELS 的 code 对齐）。 */
export const FEEDBACK_REASON_LABELS: Record<Lang, Record<string, string>> = {
  zh: {
    product_irrelevant: ZH["feedback.reason.product_irrelevant"],
    price_mismatch: ZH["feedback.reason.price_mismatch"],
    too_slow: ZH["feedback.reason.too_slow"],
    wrong_info: ZH["feedback.reason.wrong_info"],
    other: ZH["feedback.reason.other"],
  },
  en: {
    product_irrelevant: EN["feedback.reason.product_irrelevant"],
    price_mismatch: EN["feedback.reason.price_mismatch"],
    too_slow: EN["feedback.reason.too_slow"],
    wrong_info: EN["feedback.reason.wrong_info"],
    other: EN["feedback.reason.other"],
  },
};

/** 把任意输入归一化成受支持的 Lang；不是 en 一律回退 zh。 */
export function normalizeLang(value: unknown): Lang {
  if (typeof value !== "string") return DEFAULT_LANG;
  const lower = value.trim().toLowerCase();
  return lower.startsWith("en") ? "en" : "zh";
}

/**
 * 决定初始语言：优先用户上次选择，其次浏览器语言，最后默认中文。
 *
 * @param stored 本地存储里读到的值（可为 null）。
 * @param navigatorLanguage `navigator.language`（可为 undefined）。
 * @returns 归一化后的语言。
 */
export function detectLang(stored: unknown, navigatorLanguage: unknown): Lang {
  if (typeof stored === "string" && stored.trim()) {
    return normalizeLang(stored);
  }
  if (typeof navigatorLanguage === "string" && navigatorLanguage.trim()) {
    return normalizeLang(navigatorLanguage);
  }
  return DEFAULT_LANG;
}

/** 中英互切。 */
export function nextLang(lang: Lang): Lang {
  return lang === "zh" ? "en" : "zh";
}

/**
 * 取翻译文案；支持 `{name}` 占位符。
 *
 * 回退顺序：目标语言 → 中文 → key 本身（永不抛错）。
 *
 * @param lang 语言。
 * @param key 文案 key。
 * @param params 占位符参数。
 * @returns 文案。
 */
export function t(lang: Lang, key: string, params?: Record<string, string | number>): string {
  const raw = DICT[lang]?.[key] ?? DICT[DEFAULT_LANG][key] ?? key;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match,
  );
}

/** 语言对应的 5 条空状态建议问题（防御性截断到 5 条）。 */
export function suggestedQuestions(lang: Lang): readonly string[] {
  return (SUGGESTED_QUESTIONS[lang] ?? SUGGESTED_QUESTIONS[DEFAULT_LANG]).slice(0, 5);
}

/** 反馈原因 code → 本地化 label；未知 code 回退 code 本身（不编造）。 */
export function feedbackReasonLabel(lang: Lang, code: string): string {
  return FEEDBACK_REASON_LABELS[lang]?.[code] ?? code;
}

/** 列出所有翻译 key（zh 为准），用于自检 / 测试。 */
export function translationKeys(): string[] {
  return Object.keys(DICT[DEFAULT_LANG]).sort();
}

/** zh / en 字典里缺失的 key（正常情况下应为空数组）。 */
export function missingKeys(): string[] {
  const zh = new Set(Object.keys(DICT.zh));
  const en = new Set(Object.keys(DICT.en));
  return [...zh].filter((key) => !en.has(key)).concat([...en].filter((key) => !zh.has(key))).sort();
}

/** 字典条目数（以中文为准）。 */
export function translationCount(): number {
  return Object.keys(DICT[DEFAULT_LANG]).length;
}
