/**
 * P12-3 前端错误卡片与重试的纯函数 / 契约单测。
 *
 * 运行：cd frontend && node --test "tests/error-card.test.mts"
 * 由 tests/test_frontend_experience.py 以子进程方式调用，纳入 pytest 全量回归。
 *
 * 覆盖两层：
 *   1. api.ts 的 `[subagent unavailable]` 检测纯函数（final.result / AIMessage 文本）；
 *   2. App.tsx 的错误卡片 + 同 session 重试契约（源码级，避免引入 DOM 测试依赖）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  SUBAGENT_UNAVAILABLE_MARKER,
  detectSubagentFailure,
  hasSubagentUnavailable,
} from "../src/api.ts";
import { DICT, missingKeys, t, translationKeys } from "../src/i18n.ts";

const SRC = new URL("../src/", import.meta.url);
const read = (name: string): string => readFileSync(new URL(name, SRC), "utf-8");

// --------------------------------------------------------------------------- //
// 1. 标记检测纯函数
// --------------------------------------------------------------------------- //

test("subagent：标记常量与后端 SUBAGENT_UNAVAILABLE 逐字一致", () => {
  assert.equal(SUBAGENT_UNAVAILABLE_MARKER, "[subagent unavailable]");
});

test("subagent：final.result / AIMessage 文本里含标记就判定失败", () => {
  assert.equal(
    hasSubagentUnavailable("[subagent unavailable] trade_agent 连续失败 2 次：boom"),
    true,
  );
  assert.equal(
    hasSubagentUnavailable("已为你找到 3 个商品。\n[subagent unavailable] search_agent 超时"),
    true,
  );
  // 部分成功回复也命中：卡片只补充失败提示，不隐藏 partial 正文。
  const partial = "部分结果：找到 2 个商品；[subagent unavailable] 另外 1 个子任务失败";
  assert.equal(hasSubagentUnavailable(partial), true);
  assert.equal(detectSubagentFailure(partial).failed, true);
});

test("subagent：普通回复 / 非字符串一律不判定失败", () => {
  for (const value of [
    "一切正常，已下单成功",
    "",
    undefined,
    null,
    123,
    { reply: "[subagent unavailable]" },
    ["[subagent unavailable]"],
  ]) {
    assert.equal(hasSubagentUnavailable(value), false);
    assert.deepEqual(detectSubagentFailure(value), { failed: false, detail: "" });
  }
});

test("subagent：detail 取第一条含标记的整行并 trim", () => {
  const text = "第一行正常\n   [subagent unavailable] trade_agent 超时，任务 UNKNOWN   \n第三行";
  assert.deepEqual(detectSubagentFailure(text), {
    failed: true,
    detail: "[subagent unavailable] trade_agent 超时，任务 UNKNOWN",
  });
  // 没有换行时整段就是明细。
  assert.equal(
    detectSubagentFailure("[subagent unavailable] boom").detail,
    "[subagent unavailable] boom",
  );
});

// --------------------------------------------------------------------------- //
// 2. i18n：错误 / 重试文案 zh-en 都有
// --------------------------------------------------------------------------- //

test("i18n：subagent 错误与重试文案 zh / en 齐全且不同", () => {
  for (const key of ["error.subagent", "error.subagentHint", "error.retrySubagent"]) {
    assert.ok(translationKeys().includes(key), `缺少 i18n key：${key}`);
    assert.ok(t("zh", key).length > 0, `zh 文案为空：${key}`);
    assert.ok(t("en", key).length > 0, `en 文案为空：${key}`);
  }
  assert.notEqual(t("zh", "error.subagent"), t("en", "error.subagent"));
  assert.equal(t("zh", "error.retrySubagent"), DICT.zh["error.retrySubagent"]);
  assert.deepEqual(missingKeys(), []);
});

// --------------------------------------------------------------------------- //
// 3. App.tsx 契约：错误卡片 + 同一 session 重发 query
// --------------------------------------------------------------------------- //

test("App.tsx：final.result 失败标记 → error-card，partial 正文不被替换", () => {
  const app = read("App.tsx");
  assert.ok(app.includes('data-testid="error-card"'), "缺少 error-card");
  assert.ok(app.includes("SubagentErrorCard"), "缺少错误卡片组件");
  assert.ok(app.includes("detectSubagentFailure"), "未检测 [subagent unavailable]");
  // 正文先落库、再挂卡片：partial 结果照常显示。
  assert.match(app, /updateAssistant\("replace", reply, meta\);\s*\n\s*patchLastAssistant/);
});

test("App.tsx：卡片重试重发当前 query 且 session id 不变", () => {
  const app = read("App.tsx");
  assert.ok(app.includes("retrySubagent"), "缺少 retrySubagent");
  assert.ok(app.includes("retryLastError"), "既有重试入口未保留");
  // runIntent 统一用当前 sessionId 提交，重试走同一条请求路径。
  assert.match(app, /shopping_session_id: sessionId/);
  assert.match(app, /runIntent\(target, \{ retry: true \}\)/);
  // 重试不重复插入用户消息。
  assert.ok(app.includes("重试不重复插入用户消息"));
});

test("api.ts：检测函数是纯函数（不 import React / 不读 window）", () => {
  const api = read("api.ts");
  assert.ok(api.includes("export const SUBAGENT_UNAVAILABLE_MARKER"));
  assert.ok(api.includes("export function detectSubagentFailure"));
  assert.ok(api.includes("export function hasSubagentUnavailable"));
  assert.ok(!api.includes("import React"));
  assert.ok(!api.includes("window."));
});
