/**
 * P10-3 前端纯函数单测（Node 原生 test runner + 原生 TS 类型剥离，无额外依赖）。
 *
 * 运行：cd frontend && node --test "tests/*.test.mts"
 * 被 tests/test_recommendation_reasons.py 以子进程方式调用，纳入 pytest 全量回归。
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  REASON_BUDGET,
  REASON_PREFERENCE,
  REASON_SHIP_TO,
  buildComparisonRows,
  buildPreferenceHint,
  landedTotalText,
  parseProductHits,
  parseReasons,
  priceText,
  reasonTexts,
  shipsToText,
  weightText,
} from "../src/products.ts";

const CARD = {
  product_id: "P1001",
  title: "Nomadica 旅行三件套",
  brand: "Nomadica",
  category: "旅行装备",
  origin_country: "VN",
  price_major: 129,
  currency: "CNY",
  highlights: ["材质：帆布，非塑料", "重量：全套 420g 轻便"],
  ships_to: ["CN", "US"],
  landed_price: { ship_to: "US", landed_total_major: 194, currency: "CNY" },
  reasons: [
    { code: REASON_BUDGET, label: "预算命中" },
    { code: REASON_SHIP_TO, label: "可发往收货国" },
    { code: REASON_PREFERENCE, label: "命中你的偏好", matched_preference: "喜欢轻量收纳" },
  ],
};

test("parseProductHits 读商品卡并保留 reasons / ships_to", () => {
  const hits = parseProductHits({ hits: [CARD] });
  assert.equal(hits.length, 1);
  const hit = hits[0];
  assert.equal(hit.product_id, "P1001");
  assert.deepEqual(hit.ships_to, ["CN", "US"]);
  assert.deepEqual(hit.highlights, CARD.highlights);
  assert.equal(hit.reasons.length, 3);
  assert.equal(hit.reasons[2].matched_preference, "喜欢轻量收纳");
});

test("parseProductHits 兼容 result.hits 嵌套形状", () => {
  const hits = parseProductHits({ result: { hits: [CARD] } });
  assert.equal(hits.length, 1);
  assert.equal(hits[0].title, "Nomadica 旅行三件套");
});

test("不存在 reasons 字段时不编造理由", () => {
  const hits = parseProductHits({ hits: [{ product_id: "P1", title: "无理由商品" }] });
  assert.deepEqual(hits[0].reasons, []);
  assert.deepEqual(hits[0].ships_to, []);
});

test("parseReasons 只接受后端给的 code，缺 label 时退回 code", () => {
  const reasons = parseReasons([{ code: "budget_match" }, { label: "没有 code" }, null, "x"]);
  assert.deepEqual(reasons, [{ code: "budget_match", label: "budget_match" }]);
});

test("reasonTexts 偏好命中带上真实偏好原文", () => {
  const texts = reasonTexts(parseReasons(CARD.reasons as unknown));
  assert.deepEqual(texts, ["预算命中", "可发往收货国", "命中你的偏好：喜欢轻量收纳"]);
  assert.deepEqual(reasonTexts([]), []);
});

test("价格展示优先用到手价，缺到手价时退回裸价", () => {
  const hits = parseProductHits({ hits: [CARD] });
  assert.equal(priceText(hits[0]), "194 CNY");
  assert.equal(landedTotalText(hits[0]), "194 CNY");

  const bare = parseProductHits({ hits: [{ ...CARD, landed_price: undefined }] })[0];
  assert.equal(priceText(bare), "129 CNY");
  assert.equal(landedTotalText(bare), "—");
});

test("可发国家与重量缺失时用占位符，不猜数据", () => {
  assert.equal(shipsToText(parseProductHits({ hits: [CARD] })[0]), "CN / US");
  const bare = parseProductHits({ hits: [{ ...CARD, ships_to: [] }] })[0];
  assert.equal(shipsToText(bare), "—");
  assert.equal(weightText(CARD.highlights), "全套 420g 轻便");
  assert.equal(weightText(["材质：帆布"]), "—");
});

test("buildComparisonRows 用后端 top_picks 生成前 3 行", () => {
  const payload = {
    top_picks: [
      {
        product_id: "P1001",
        title: "Nomadica 旅行三件套",
        price_major: 129,
        currency: "CNY",
        landed_total_major: 194,
        weight: "全套 420g 轻便",
        origin_country: "VN",
        ships_to: ["CN", "US"],
        highlights: ["材质：帆布，非塑料"],
        reason_labels: ["预算命中"],
      },
      {
        product_id: "P1002",
        title: "TrailOx 20寸登机箱",
        price_major: 899,
        currency: "CNY",
        landed_total_major: null,
        weight: null,
        origin_country: "DE",
        ships_to: ["CN", "EU"],
        highlights: [],
        reason_labels: [],
      },
    ],
  };
  const rows = buildComparisonRows(payload);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], {
    product_id: "P1001",
    title: "Nomadica 旅行三件套",
    price: "129 CNY",
    landedTotal: "194 CNY",
    weight: "全套 420g 轻便",
    originCountry: "VN",
    shipsTo: "CN / US",
    highlights: ["材质：帆布，非塑料"],
    reasons: ["预算命中"],
  });
  assert.equal(rows[1].weight, "—");
  assert.equal(rows[1].landedTotal, "—");
});

test("对比表区分美元标价与人民币到手价", () => {
  const rows = buildComparisonRows({top_picks: [{product_id: "P1010", title: "CascadePro",
    price_major: 149, currency: "USD", landed_total_major: 1132.9, landed_currency: "CNY"}]});
  assert.equal(rows[0].price, "149 USD");
  assert.equal(rows[0].landedTotal, "1132.9 CNY");
});

test("buildComparisonRows 无 top_picks 时用 hits 兜底（最多 3 行）", () => {
  const rows = buildComparisonRows({ hits: [CARD, CARD, CARD, CARD] });
  assert.equal(rows.length, 3);
  assert.equal(rows[0].price, "129 CNY");
  assert.equal(rows[0].landedTotal, "194 CNY");
  assert.equal(rows[0].weight, "全套 420g 轻便");
  assert.deepEqual(rows[0].reasons, ["预算命中", "可发往收货国", "命中你的偏好：喜欢轻量收纳"]);

  assert.deepEqual(buildComparisonRows({}), []);
});

test("buildPreferenceHint 只提交显式选择的槽位", () => {
  assert.equal(buildPreferenceHint({ budgetMaxMajor: null, budgetCurrency: "CNY", shipTo: "", category: "" }), null);
  assert.deepEqual(
    buildPreferenceHint({ budgetMaxMajor: 200, budgetCurrency: "CNY", shipTo: "US", category: "旅行装备" }),
    { budget_max_major: 200, budget_currency: "CNY", ship_to: "US", category: "旅行装备" },
  );
  assert.deepEqual(
    buildPreferenceHint({ budgetMaxMajor: null, budgetCurrency: "CNY", shipTo: "US", category: "" }),
    { budget_currency: "CNY", ship_to: "US" },
  );
  // 0 / NaN / 负数预算视为"不限"，不把无效值发给后端。
  assert.equal(
    buildPreferenceHint({ budgetMaxMajor: 0, budgetCurrency: "CNY", shipTo: "", category: "" }),
    null,
  );
});
