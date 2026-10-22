/**
 * P10-2 前端纯函数单测（Node 原生 test runner + 原生 TS 类型剥离，无额外依赖）。
 *
 * 运行：cd frontend && node --test "tests/*.test.mts"
 * 被 tests/test_order_preview_flow.py 以子进程方式调用，纳入 pytest 全量回归。
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  PREVIEW_STATE,
  applyPreviewAction,
  buildPreviewFromInvocation,
  buildPreviewFromOrderPreview,
  buildResultCard,
  eventTraceId,
  extractEvidence,
  findResultForTrace,
  formatAddress,
  parseCreateOrderInvocation,
  parseOrderPreview,
  parseOrderToolResult,
  verificationBadge,
} from "../src/orderPreview.ts";

const INVOKE_EVENT = {
  tool: "create_order",
  trace_id: "trace-abc",
  args: {
    items: [{ product_id: "P1001", sku_id: "P1001-S1", quantity: 2 }],
    shipping_address: {
      recipient_name: "张三",
      country: "CN",
      state: "浙江",
      city: "杭州",
      address_line: "某路 1 号",
      postal_code: "310000",
      phone: "13800000000",
    },
  },
};

/** Task 2 修复后的主路径事件：后端已挂起等待 token 决议。 */
const ORDER_PREVIEW_EVENT = {
  tool: "create_order",
  token: "tok-abc123",
  trace_id: "trace-preview",
  preview: {
    items: [{ product_id: "P1001", sku_id: "P1001-S1", quantity: 2 }],
    shipping_address: {
      recipient_name: "张三",
      country: "CN",
      state: "浙江",
      city: "杭州",
      address_line: "某路 1 号",
      postal_code: "310000",
      phone: "13800000000",
    },
  },
};

const RESULT_ORDER = {
  order_id: "GBX-000001",
  status: "CONFIRMED",
  total_amount_major: 199.8,
  currency: "CNY",
  shipping_address: "张三 · 13800000000 · CN 浙江 杭州 · 某路 1 号 · 310000",
  lines: [
    {
      product_id: "P1001",
      sku_id: "P1001-S1",
      title: "行李箱（24 寸）",
      unit_price_major: 99.9,
      quantity: 2,
    },
  ],
  created_at: "2026-10-10T11:00:00+00:00",
  cancel_reason: "",
  verification_status: "VERIFIED",
};

// --------------------------------------------------------------------------
// 1. tool.invoke 事件解析
// --------------------------------------------------------------------------
test("parseCreateOrderInvocation 解析 create_order 入参", () => {
  const args = parseCreateOrderInvocation(INVOKE_EVENT);
  assert.ok(args);
  assert.equal(args.items.length, 1);
  assert.deepEqual(args.items[0], { product_id: "P1001", sku_id: "P1001-S1", quantity: 2 });
  assert.equal(args.shipping_address.city, "杭州");
});

test("parseCreateOrderInvocation 忽略非 create_order / 残缺载荷", () => {
  assert.equal(parseCreateOrderInvocation(null), null);
  assert.equal(parseCreateOrderInvocation({ tool: "product_search", args: {} }), null);
  assert.equal(parseCreateOrderInvocation({ tool: "create_order" }), null);
  assert.equal(parseCreateOrderInvocation({ tool: "create_order", args: { items: [] } }), null);
  assert.equal(
    parseCreateOrderInvocation({ tool: "create_order", args: { items: [{ product_id: "P1" }] } }),
    null,
  );
});

test("parseCreateOrderInvocation 对数量缺省补 1，不编造商品", () => {
  const args = parseCreateOrderInvocation({
    tool: "create_order",
    args: {
      items: [{ product_id: "P1001", sku_id: "P1001-S1" }],
      shipping_address: { city: "杭州" },
    },
  });
  assert.ok(args);
  assert.equal(args.items[0].quantity, 1);
});

// --------------------------------------------------------------------------
// 2. 订单预览卡片数据
// --------------------------------------------------------------------------
test("buildPreviewFromInvocation 展示商品 / SKU / 数量 / 地址", () => {
  const preview = buildPreviewFromInvocation(parseCreateOrderInvocation(INVOKE_EVENT));
  assert.ok(preview);
  assert.equal(preview.items.length, 1);
  assert.equal(preview.items[0].title, "P1001");
  assert.equal(preview.items[0].sku_id, "P1001-S1");
  assert.equal(preview.items[0].quantity, 2);
  assert.equal(preview.item_count, 2);
  assert.match(preview.address_text, /张三/);
  assert.match(preview.address_text, /杭州/);
  // 入参没有单价：金额留给后端，前端不编造。
  assert.equal(preview.amount_known, false);
  assert.equal(preview.subtotal_major, undefined);
  assert.equal(preview.total_major, undefined);
  assert.equal(preview.status, "PENDING");
});

test("buildPreviewFromInvocation 有单价时计算小计与合计", () => {
  const preview = buildPreviewFromInvocation({
    currency: "CNY",
    shipping_address: { recipient_name: "李四", city: "上海", address_line: "南京路 2 号" },
    items: [
      { product_id: "P1", sku_id: "P1-S1", quantity: 2, unit_price_major: 10.5 },
      { product_id: "P2", sku_id: "P2-S1", quantity: 1, unit_price_major: 5 },
    ],
  });
  assert.ok(preview);
  assert.equal(preview.amount_known, true);
  assert.equal(preview.subtotal_major, 26);
  assert.equal(preview.total_major, 26);
  assert.equal(preview.currency, "CNY");
  assert.equal(preview.items[0].line_total_major, 21);
});

test("formatAddress 兼容字符串与对象", () => {
  assert.equal(formatAddress("张三 · 杭州"), "张三 · 杭州");
  assert.equal(formatAddress(""), "");
  assert.equal(formatAddress(null), "");
  assert.equal(formatAddress({ recipient_name: "王五", city: "北京" }), "王五 · 北京");
});

// --------------------------------------------------------------------------
// 3. 确认 / 取消分支（真实 token 握手）
// --------------------------------------------------------------------------
test("applyPreviewAction 确认前不产出任何动作（后端仍在挂起等待）", () => {
  const preview = { state: PREVIEW_STATE.PENDING, order_id: "", token: "tok-1" };
  assert.deepEqual(applyPreviewAction(preview, null), { state: "pending", action: null });
});

test("applyPreviewAction 点击确认产出 confirm_order（带 token 调 /commerce/orders/confirm）", () => {
  const preview = { state: PREVIEW_STATE.PENDING, order_id: "", token: "tok-1" };
  const decided = applyPreviewAction(preview, "confirm");
  assert.equal(decided.state, "confirmed");
  assert.deepEqual(decided.action, {
    type: "confirm_order",
    token: "tok-1",
    endpoint: "/api/commerce/orders/confirm",
  });
});

test("applyPreviewAction 点击取消产出 cancel_order（不触发下单）", () => {
  const preview = { state: PREVIEW_STATE.PENDING, order_id: "", token: "tok-2" };
  const decided = applyPreviewAction(preview, "cancel");
  assert.equal(decided.state, "cancelled");
  assert.deepEqual(decided.action, {
    type: "cancel_order",
    token: "tok-2",
    endpoint: "/api/commerce/orders/confirm",
  });
});

test("applyPreviewAction 无 token 的旧 invoke 预览：取消只本地放弃", () => {
  const decided = applyPreviewAction(
    { state: PREVIEW_STATE.PENDING, order_id: "" },
    "cancel",
  );
  assert.equal(decided.state, "cancelled");
  assert.deepEqual(decided.action, { type: "abandon_preview" });
});

test("applyPreviewAction 无 token 且已有 order_id：确认只做幂等查单", () => {
  const decided = applyPreviewAction(
    { state: PREVIEW_STATE.PENDING, order_id: "GBX-000001" },
    "confirm",
  );
  assert.equal(decided.state, "confirmed");
  assert.deepEqual(decided.action, {
    type: "query_order",
    order_id: "GBX-000001",
    endpoint: "/api/commerce/orders/GBX-000001",
    label: "查询订单",
  });
});

test("applyPreviewAction 已确认后重复点击只做查单", () => {
  const decided = applyPreviewAction(
    { state: PREVIEW_STATE.CONFIRMED, order_id: "GBX-000001" },
    "confirm",
  );
  assert.equal(decided.state, "confirmed");
  assert.equal(decided.action?.type, "query_order");
  assert.equal(decided.action?.order_id, "GBX-000001");
});

test("applyPreviewAction 空闲态不产出动作", () => {
  assert.deepEqual(applyPreviewAction({ state: PREVIEW_STATE.IDLE, order_id: "" }, "confirm").action, null);
  assert.deepEqual(applyPreviewAction(null, "cancel").action, null);
});

// --------------------------------------------------------------------------
// 4. order.preview 真实握手事件（Task 2 修复主路径）
// --------------------------------------------------------------------------
test("parseOrderPreview 解析 token + preview", () => {
  const parsed = parseOrderPreview(ORDER_PREVIEW_EVENT);
  assert.ok(parsed);
  assert.equal(parsed.token, "tok-abc123");
  assert.equal(parsed.trace_id, "trace-preview");
  assert.equal(parsed.preview.items.length, 1);
  assert.equal(parsed.preview.items[0].sku_id, "P1001-S1");
  assert.equal(parsed.preview.shipping_address.city, "杭州");
});

test("parseOrderPreview 拒绝缺 token / 缺 preview / 非 create_order 事件", () => {
  assert.equal(parseOrderPreview(null), null);
  assert.equal(parseOrderPreview({ ...ORDER_PREVIEW_EVENT, tool: "product_search" }), null);
  assert.equal(parseOrderPreview({ ...ORDER_PREVIEW_EVENT, token: "" }), null);
  assert.equal(parseOrderPreview({ ...ORDER_PREVIEW_EVENT, preview: null }), null);
  assert.equal(
    parseOrderPreview({ ...ORDER_PREVIEW_EVENT, preview: { items: [], shipping_address: {} } }),
    null,
  );
});

test("buildPreviewFromOrderPreview 渲染商品 / SKU / 数量 / 地址且不编造金额", () => {
  const card = buildPreviewFromOrderPreview(ORDER_PREVIEW_EVENT);
  assert.ok(card);
  assert.equal(card.source, "preview");
  assert.equal(card.order_id, "");
  assert.equal(card.status, "PENDING");
  assert.equal(card.item_count, 2);
  assert.equal(card.items[0].sku_id, "P1001-S1");
  assert.match(card.address_text, /杭州/);
  assert.equal(card.amount_known, false);
  assert.equal(card.total_major, undefined);
});

test("order.preview 载荷不含单价（后端只回传已知信息）", () => {
  const payload = JSON.parse(JSON.stringify(ORDER_PREVIEW_EVENT)) as {
    preview: { items: Array<Record<string, unknown>> };
  };
  for (const item of payload.preview.items) {
    assert.equal("unit_price_major" in item, false);
    assert.equal("unit_price" in item, false);
  }
  const card = buildPreviewFromOrderPreview(payload);
  assert.ok(card);
  assert.equal(card.amount_known, false);
});

// --------------------------------------------------------------------------
// 5. 证据徽章映射
// --------------------------------------------------------------------------
test("verificationBadge 把 VERIFIED 映射为绿色已验证", () => {
  const badge = verificationBadge("VERIFIED");
  assert.equal(badge.tone, "green");
  assert.equal(badge.label, "已验证");
  assert.equal(badge.queryable, false);
});

test("verificationBadge 把 UNKNOWN / EXECUTED / UNVERIFIED 映射为结果待确认", () => {
  for (const status of ["UNKNOWN", "unknown", "EXECUTED", "UNVERIFIED", undefined, "WHAT"]) {
    const badge = verificationBadge(status);
    assert.equal(badge.tone, "amber", `status=${status}`);
    assert.equal(badge.label, "结果待确认", `status=${status}`);
    assert.equal(badge.queryable, true, `status=${status}`);
  }
});

test("extractEvidence 从工具结果取 status / order_id / verification_status", () => {
  const evidence = extractEvidence(RESULT_ORDER);
  assert.equal(evidence.order_id, "GBX-000001");
  assert.equal(evidence.status, "CONFIRMED");
  assert.equal(evidence.status_label, "已确认");
  assert.equal(evidence.verification_status, "VERIFIED");
  assert.equal(evidence.badge.tone, "green");
});

test("extractEvidence 缺 verification_status 时降级为待确认", () => {
  const evidence = extractEvidence({ order_id: "GBX-000002", status: "CONFIRMED" });
  assert.equal(evidence.verification_status, "UNKNOWN");
  assert.equal(evidence.badge.label, "结果待确认");
  assert.equal(evidence.badge.queryable, true);
});

// --------------------------------------------------------------------------
// 6. 订单结果卡片
// --------------------------------------------------------------------------
test("buildResultCard 汇总金额与行项目", () => {
  const card = buildResultCard(RESULT_ORDER);
  assert.ok(card);
  assert.equal(card.order_id, "GBX-000001");
  assert.equal(card.status_label, "已确认");
  assert.equal(card.item_count, 2);
  assert.equal(card.subtotal_major, 199.8);
  assert.equal(card.total_major, 199.8);
  assert.equal(card.currency, "CNY");
  assert.equal(card.items[0].title, "行李箱（24 寸）");
});

test("buildResultCard 解析取消订单状态", () => {
  const card = buildResultCard({ ...RESULT_ORDER, status: "CANCELLED", cancel_reason: "用户取消" });
  assert.ok(card);
  assert.equal(card.status_label, "已取消");
  assert.equal(card.cancel_reason, "用户取消");
});

test("buildResultCard 对非订单载荷返回 null", () => {
  assert.equal(buildResultCard(null), null);
  assert.equal(buildResultCard({}), null);
});

// --------------------------------------------------------------------------
// 7. 事件关联
// --------------------------------------------------------------------------
test("parseOrderToolResult 识别 create_order 结果与错误", () => {
  const ok = parseOrderToolResult({ tool: "create_order", order: RESULT_ORDER });
  assert.ok(ok?.order);
  assert.equal(ok.order.order_id, "GBX-000001");
  const failed = parseOrderToolResult({ tool: "create_order", error: "商品不存在" });
  assert.deepEqual(failed, { error: "商品不存在" });
  assert.equal(parseOrderToolResult({ tool: "product_search", hits: [] }), null);
});

test("eventTraceId / findResultForTrace 按 trace 关联本轮结果", () => {
  assert.equal(eventTraceId({ trace_id: "t1" }), "t1");
  assert.equal(eventTraceId(null), "");

  const events = [
    { event: "tool.invoke", payload: { tool: "create_order", trace_id: "t-old" } },
    { event: "tool.result", payload: { tool: "query_order", trace_id: "t1", order: { order_id: "GBX-9" } } },
    { event: "tool.result", payload: { tool: "create_order", trace_id: "t1", order: RESULT_ORDER } },
  ];
  const found = findResultForTrace(events, "t1");
  assert.ok(found);
  assert.equal(found.order_id, "GBX-000001");
  assert.equal(findResultForTrace(events, "t-missing"), null);
});

// --------------------------------------------------------------------------
// 8. REST 契约：确认接口 URL / 请求体 / 响应解析
// --------------------------------------------------------------------------
test("confirmOrder 调 POST /api/commerce/orders/confirm 并回传 token+action", async () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({ token: "tok-9", status: "confirmed", ok: true }),
    };
  }) as unknown as typeof fetch;

  try {
    const { confirmOrder } = await import("../src/api.ts");
    const response = await confirmOrder("tok-9", "confirm");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/commerce/orders/confirm");
    assert.equal(calls[0].init.method, "POST");
    assert.deepEqual(JSON.parse(String(calls[0].init.body)), {
      token: "tok-9",
      action: "confirm",
    });
    assert.equal(response.status, "confirmed");
    assert.equal(response.ok, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("confirmOrder 在 HTTP 错误时抛出 detail", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: false,
    status: 422,
    json: async () => ({ detail: "非法动作" }),
  })) as unknown as typeof fetch;

  try {
    const { confirmOrder } = await import("../src/api.ts");
    await assert.rejects(() => confirmOrder("tok-x", "confirm"), /非法动作/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
