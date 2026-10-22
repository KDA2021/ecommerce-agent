/**
 * P10-3 个性化与可解释推荐的前端纯函数。
 *
 * 分成三块：
 *   1. 商品卡解析：把后端 `product_search` 结果里的 hits / top_picks 读成结构化数据；
 *   2. 推荐理由展示：reason code → 可读文案，**只渲染后端给的理由**；
 *   3. 对比表行列：前 3 个商品的价格 / 重量 / 可发国家 / 亮点 / 理由。
 *
 * 这里不猜任何数据：后端没给的字段一律显示占位符（—），不自己编价格或重量。
 */

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** 推荐理由 code（与后端 recommendation_reasons.REASON_* 一致）。 */
export const REASON_BUDGET = "budget_match";
export const REASON_SHIP_TO = "ship_to_match";
export const REASON_PREFERENCE = "preference_match";

/** 单条推荐理由：后端只输出真实命中的事实。 */
export interface RecommendationReason {
  code: string;
  label: string;
  /** 仅 preference_match 带：真实命中的偏好原文。 */
  matched_preference?: string;
}

export interface ProductHit {
  product_id: string;
  title: string;
  brand?: string;
  category?: string;
  origin_country?: string;
  price_major?: number;
  currency?: string;
  highlights: string[];
  ships_to: string[];
  landed_price?: Record<string, unknown>;
  reasons: RecommendationReason[];
}

/** 对比视图一行（由后端 top_picks 直接给出，避免前端重复算）。 */
export interface ComparisonRow {
  product_id: string;
  title: string;
  price: string;
  landedTotal: string;
  weight: string;
  originCountry: string;
  shipsTo: string;
  highlights: string[];
  reasons: string[];
}

/** 前端偏好 chips 选择。 */
export interface PreferenceSelection {
  budgetMaxMajor: number | null;
  budgetCurrency: string;
  shipTo: string;
  category: string;
}

export const EMPTY_PREFERENCE: PreferenceSelection = {
  budgetMaxMajor: null,
  budgetCurrency: "CNY",
  shipTo: "",
  category: "",
};

export const BUDGET_CHIPS = [100, 200, 500, 1000] as const;
export const COUNTRY_CHIPS = ["CN", "US", "EU", "JP"] as const;
export const CATEGORY_CHIPS = ["旅行装备", "数码配件", "家居生活", "户外运动"] as const;

const PLACEHOLDER = "—";

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as JsonRecord;
}

function readString(record: JsonRecord | null, key: string): string | undefined {
  const value = record?.[key];
  return typeof value === "string" ? value : undefined;
}

function readNumber(record: JsonRecord | null, key: string): number | undefined {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function readStringArray(record: JsonRecord | null, key: string): string[] {
  const value = record?.[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

/** 读推荐理由；后端只给真实命中的，这里不做任何补充。 */
export function parseReasons(value: unknown): RecommendationReason[] {
  if (!Array.isArray(value)) return [];
  const reasons: RecommendationReason[] = [];
  for (const item of value) {
    const record = asRecord(item);
    if (!record) continue;
    const code = readString(record, "code");
    if (!code) continue;
    const matched = readString(record, "matched_preference");
    reasons.push({
      code,
      label: readString(record, "label") ?? code,
      ...(matched ? { matched_preference: matched } : {}),
    });
  }
  return reasons;
}

function parseHit(value: unknown): ProductHit | null {
  const record = asRecord(value);
  if (!record) return null;
  const productId = readString(record, "product_id");
  const title = readString(record, "title");
  if (!productId && !title) return null;
  const landed = asRecord(record.landed_price);
  return {
    product_id: productId ?? "",
    title: title ?? "未命名商品",
    brand: readString(record, "brand"),
    category: readString(record, "category"),
    origin_country: readString(record, "origin_country"),
    price_major: readNumber(record, "price_major"),
    currency: readString(record, "currency"),
    highlights: readStringArray(record, "highlights"),
    ships_to: readStringArray(record, "ships_to"),
    ...(landed ? { landed_price: landed } : {}),
    reasons: parseReasons(record.reasons),
  };
}

/** 从 tool.result / result 载荷里读商品卡（兼容直接 hits 与嵌套 result.hits）。 */
export function parseProductHits(payload: unknown): ProductHit[] {
  const record = asRecord(payload);
  const direct = record?.hits;
  const nested = asRecord(record?.result)?.hits;
  const raw = Array.isArray(direct) ? direct : Array.isArray(nested) ? nested : [];
  const hits: ProductHit[] = [];
  for (const item of raw) {
    const hit = parseHit(item);
    if (hit) hits.push(hit);
  }
  return hits;
}

// ---------------------------------------------------------------------------
// 展示文案
// ---------------------------------------------------------------------------

/** 单条理由文案：预算/可发用后端 label，偏好命中额外带上真实命中的偏好原文。 */
export function reasonText(reason: RecommendationReason): string {
  if (reason.code === REASON_PREFERENCE && reason.matched_preference) {
    return `${reason.label}：${reason.matched_preference}`;
  }
  return reason.label || reason.code;
}

/** 所有理由的展示文案；无理由时返回空数组（不渲染空标签）。 */
export function reasonTexts(reasons: RecommendationReason[]): string[] {
  return reasons.map(reasonText).filter((text) => text.length > 0);
}

function formatMoney(amount: number | undefined, currency: string | undefined): string {
  if (typeof amount !== "number") return PLACEHOLDER;
  return currency ? `${amount} ${currency}` : String(amount);
}

/** 到手价展示；未命中 ship_to（后端没给 landed_price）时返回占位符。 */
export function landedTotalText(hit: ProductHit): string {
  const landed = hit.landed_price;
  const landedTotal = typeof landed?.landed_total_major === "number" ? landed.landed_total_major : undefined;
  if (landedTotal === undefined) return PLACEHOLDER;
  const currency = readString(landed ?? null, "currency") ?? hit.currency;
  return formatMoney(landedTotal, currency);
}

/** 价格展示：有 ship_to 时优先用到手价。 */
export function priceText(hit: ProductHit): string {
  const landed = landedTotalText(hit);
  return landed === PLACEHOLDER ? formatMoney(hit.price_major, hit.currency) : landed;
}

/** 可发国家展示；后端没给就显示占位符，不猜。 */
export function shipsToText(hit: ProductHit): string {
  return hit.ships_to.length > 0 ? hit.ships_to.join(" / ") : PLACEHOLDER;
}

/**
 * 从亮点里取重量标注（如"重量：全套 420g 轻便"）；没有就返回占位符。
 *
 * @param highlights 已格式化的亮点文案。
 * @returns 重量文本。
 */
export function weightText(highlights: string[]): string {
  for (const highlight of highlights) {
    if (highlight.startsWith("重量") && highlight.includes("：")) {
      return highlight.slice(highlight.indexOf("：") + 1).trim() || PLACEHOLDER;
    }
  }
  return PLACEHOLDER;
}

// ---------------------------------------------------------------------------
// 对比视图（前 3 个商品）
// ---------------------------------------------------------------------------

/** 前端对比条数：与后端 top_picks 上限保持一致。 */
export const COMPARISON_LIMIT = 3;

/**
 * 取前 3 个商品构造对比行。
 *
 * 优先用后端给的 `top_picks`（字段已对齐）；没有时用 hits 兜底，保证老版本后端
 * 也能渲染对比表。
 *
 * @param payload tool.result / 检索结果载荷。
 * @returns 对比行列表（最多 3 行）。
 */
export function buildComparisonRows(payload: unknown): ComparisonRow[] {
  const record = asRecord(payload);
  const picks = record?.top_picks;
  if (Array.isArray(picks) && picks.length > 0) {
    const rows: ComparisonRow[] = [];
    for (const item of picks.slice(0, COMPARISON_LIMIT)) {
      const pick = asRecord(item);
      if (!pick) continue;
      const priceMajor = readNumber(pick, "price_major");
      const landed = readNumber(pick, "landed_total_major");
      rows.push({
        product_id: readString(pick, "product_id") ?? "",
        title: readString(pick, "title") ?? "未命名商品",
        price: formatMoney(priceMajor, readString(pick, "currency")),
        landedTotal: formatMoney(landed, readString(pick, "currency")),
        weight: readString(pick, "weight") ?? PLACEHOLDER,
        originCountry: readString(pick, "origin_country") ?? PLACEHOLDER,
        shipsTo: readStringArray(pick, "ships_to").join(" / ") || PLACEHOLDER,
        highlights: readStringArray(pick, "highlights"),
        reasons: readStringArray(pick, "reason_labels"),
      });
    }
    return rows;
  }
  return parseProductHits(payload)
    .slice(0, COMPARISON_LIMIT)
    .map((hit) => ({
      product_id: hit.product_id,
      title: hit.title,
      price: formatMoney(hit.price_major, hit.currency),
      landedTotal: landedTotalText(hit),
      weight: weightText(hit.highlights),
      originCountry: hit.origin_country ?? PLACEHOLDER,
      shipsTo: shipsToText(hit),
      highlights: hit.highlights,
      reasons: reasonTexts(hit.reasons),
    }));
}

// ---------------------------------------------------------------------------
// 偏好 chips → 请求体
// ---------------------------------------------------------------------------

/** 把 chips 选择转成随请求提交的 preference_hint；没选任何项时返回 null。 */
export function buildPreferenceHint(
  selection: PreferenceSelection,
): { budget_max_major: number; budget_currency: string; ship_to?: string; category?: string } | null {
  const hint: {
    budget_max_major: number;
    budget_currency: string;
    ship_to?: string;
    category?: string;
  } = {
    budget_max_major: 0,
    budget_currency: selection.budgetCurrency || "CNY",
  };
  let hasBudget = false;
  if (typeof selection.budgetMaxMajor === "number" && Number.isFinite(selection.budgetMaxMajor) && selection.budgetMaxMajor > 0) {
    hint.budget_max_major = selection.budgetMaxMajor;
    hasBudget = true;
  }
  if (selection.shipTo) hint.ship_to = selection.shipTo;
  if (selection.category) hint.category = selection.category;
  if (!hasBudget) delete (hint as { budget_max_major?: number }).budget_max_major;
  const hasAny = hasBudget || Boolean(selection.shipTo) || Boolean(selection.category);
  return hasAny ? hint : null;
}
