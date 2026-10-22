/**
 * P10-4 前端体验补全的纯函数单测（Node 原生 test runner + 原生 TS 类型剥离）。
 *
 * 运行：cd frontend && node --test "tests/*.test.mts"
 * 由 tests/test_frontend_experience.py 以子进程方式调用，纳入 pytest 全量回归。
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  DICT,
  FEEDBACK_REASON_LABELS,
  LANG_STORAGE_KEY,
  detectLang,
  feedbackReasonLabel,
  missingKeys,
  nextLang,
  normalizeLang,
  suggestedQuestions,
  t,
  translationCount,
  translationKeys,
} from "../src/i18n.ts";
import {
  MAX_STORED_SESSIONS,
  SESSION_STORE_KEY,
  mergeBackendSessions,
  mergeHistory,
  newSessionId,
  normalizeThreadMessages,
  parseSessionStore,
  removeSession,
  serializeSessionStore,
  titleFromQuery,
  toEpochMs,
  upsertSession,
} from "../src/sessionHistory.ts";

// --------------------------------------------------------------------------- //
// i18n
// --------------------------------------------------------------------------- //

test("i18n：字典至少 15 条，且 zh / en key 完全对齐", () => {
  assert.ok(translationCount() >= 15, `字典只有 ${translationCount()} 条`);
  assert.deepEqual(missingKeys(), []);
  assert.deepEqual(Object.keys(DICT.zh).sort(), Object.keys(DICT.en).sort());
  assert.ok(translationKeys().includes("chat.send"));
  assert.ok(translationKeys().includes("error.retry"));
  assert.equal(LANG_STORAGE_KEY, "ecommerce-agent.lang");
});

test("i18n：t 返回对应语言文案并支持 {name} 占位符", () => {
  assert.equal(t("zh", "chat.send"), "发送");
  assert.equal(t("en", "chat.send"), "Send");
  assert.equal(t("zh", "history.turns", { count: 3 }), "3 轮");
  assert.equal(t("en", "history.turns", { count: 3 }), "3 turns");
  assert.equal(t("en", "order.itemCount", { count: 2 }), "2 items");
  // 未知 key 回退 key 本身，不抛错。
  assert.equal(t("en", "no.such.key"), "no.such.key");
  // 未知占位符保持原样，不丢内容。
  assert.equal(t("zh", "history.turns", { other: 1 }), "{count} 轮");
});

test("i18n：normalizeLang / detectLang / nextLang", () => {
  assert.equal(normalizeLang("EN-US"), "en");
  assert.equal(normalizeLang("zh-CN"), "zh");
  assert.equal(normalizeLang(undefined), "zh");
  assert.equal(detectLang("en", "zh-CN"), "en");
  assert.equal(detectLang(null, "en-GB"), "en");
  assert.equal(detectLang("", undefined), "zh");
  assert.equal(nextLang("zh"), "en");
  assert.equal(nextLang("en"), "zh");
});

test("i18n：空状态每种语言恰好 5 条建议问题且不重复", () => {
  for (const lang of ["zh", "en"] as const) {
    const questions = suggestedQuestions(lang);
    assert.equal(questions.length, 5, `${lang} 建议问题不是 5 条`);
    assert.equal(new Set(questions).size, 5);
    for (const question of questions) {
      assert.ok(question.trim().length > 0);
    }
  }
  assert.notDeepEqual(suggestedQuestions("zh"), suggestedQuestions("en"));
});

test("i18n：反馈原因 code 本地化，未知 code 原样返回", () => {
  for (const lang of ["zh", "en"] as const) {
    for (const code of Object.keys(FEEDBACK_REASON_LABELS[lang])) {
      assert.ok(feedbackReasonLabel(lang, code).length > 0);
    }
  }
  assert.equal(feedbackReasonLabel("zh", "too_slow"), "回答太慢");
  assert.equal(feedbackReasonLabel("en", "too_slow"), "Too slow");
  assert.equal(feedbackReasonLabel("en", "unknown_code"), "unknown_code");
});

// --------------------------------------------------------------------------- //
// sessionHistory：localStorage 兜底
// --------------------------------------------------------------------------- //

test("sessionHistory：坏 JSON / 空值解析成空列表", () => {
  assert.deepEqual(parseSessionStore(null), []);
  assert.deepEqual(parseSessionStore(""), []);
  assert.deepEqual(parseSessionStore("{bad json"), []);
  assert.deepEqual(parseSessionStore('{"not":"array"}'), []);
});

test("sessionHistory：解析存储、丢弃坏条目、按 updatedAt 倒序、消息截断", () => {
  const raw = JSON.stringify([
    {
      id: "s-old",
      title: "旧会话",
      updatedAt: 1000,
      messages: [
        { id: "m1", role: "user", content: "你好" },
        { id: "m2", role: "tool", content: "内部消息" },
        { id: "m3", role: "assistant", content: "  " },
        { id: "m4", role: "assistant", content: "在的" },
      ],
    },
    { id: "s-new", title: "新会话", updatedAt: 2000, messages: [] },
    { id: "", title: "没 id", updatedAt: 3000 },
    { nope: true },
  ]);
  const sessions = parseSessionStore(raw);
  assert.deepEqual(
    sessions.map((session) => session.id),
    ["s-new", "s-old"],
  );
  assert.deepEqual(
    sessions[1].messages.map((message) => message.id),
    ["m1", "m4"],
    "tool / 空内容消息应被丢弃",
  );
  assert.equal(sessions[1].source, undefined);
});

test("sessionHistory：serialize → parse 往返一致，且超量按时间倒序截断", () => {
  const many = Array.from({ length: MAX_STORED_SESSIONS + 5 }, (_, index) => ({
    id: `s-${index}`,
    title: `t-${index}`,
    updatedAt: 1_700_000_000_000 + index,
    messages: [{ id: "m", role: "user" as const, content: "hi" }],
    source: "local" as const,
  }));
  const serialized = serializeSessionStore(many);
  const parsed = parseSessionStore(serialized);
  assert.equal(parsed.length, MAX_STORED_SESSIONS);
  assert.equal(parsed[0].id, `s-${MAX_STORED_SESSIONS + 4}`);
  assert.equal(serialized, serializeSessionStore(parsed), "稳定幂等");
  assert.ok(SESSION_STORE_KEY.startsWith("ecommerce-agent."));
});

test("sessionHistory：upsert 更新已有会话并置顶，source 合并成 both", () => {
  const base = [
    { id: "a", title: "A", updatedAt: 10, messages: [], source: "backend" as const },
    { id: "b", title: "B", updatedAt: 20, messages: [] },
  ];
  const next = upsertSession(base, {
    id: "a",
    title: "A2",
    updatedAt: 30,
    messages: [{ id: "m1", role: "user", content: "hi" }],
  });
  assert.equal(next.length, 2);
  assert.equal(next[0].id, "a");
  assert.equal(next[0].title, "A2");
  assert.equal(next[0].source, "both");
  assert.equal(next[0].messages.length, 1);
});

test("sessionHistory：removeSession 只删指定 id", () => {
  const sessions = [
    { id: "a", title: "A", updatedAt: 1, messages: [] },
    { id: "b", title: "B", updatedAt: 2, messages: [] },
  ];
  assert.deepEqual(removeSession(sessions, "a").map((s) => s.id), ["b"]);
  assert.deepEqual(removeSession(sessions, "missing").map((s) => s.id), ["a", "b"]);
});

test("sessionHistory：标题取首条提问、压缩空白并截断", () => {
  assert.equal(titleFromQuery("  帮我   找旅行三件套 ", "兜底"), "帮我 找旅行三件套");
  assert.equal(titleFromQuery("", "兜底"), "兜底");
  assert.equal(titleFromQuery("   ", "兜底"), "兜底");
  assert.equal(titleFromQuery("一二三四五六", "兜底", 3), "一二三…");
});

test("sessionHistory：时间戳秒 / 毫秒统一成毫秒", () => {
  assert.equal(toEpochMs(1_700_000_000), 1_700_000_000_000);
  assert.equal(toEpochMs(1_700_000_000_000), 1_700_000_000_000);
  assert.equal(toEpochMs(undefined, 42), 42);
  assert.equal(toEpochMs(0, 42), 42);
});

// --------------------------------------------------------------------------- //
// sessionHistory：后端 thread 优先
// --------------------------------------------------------------------------- //

test("sessionHistory：后端列表补全本地没有的会话，并在两边都有时记 both", () => {
  const local = [{ id: "s-local", title: "本地", updatedAt: 1000, messages: [], source: "local" as const }];
  const merged = mergeBackendSessions(
    local,
    [
      { session_id: "s-backend", turns: 2, updated_at: 1_700_000_000, preview: "这是我最近的回复" },
      { session_id: "s-local", turns: 5, updated_at: 1_700_000_100 },
    ],
    "未命名会话",
  );
  assert.deepEqual(merged.map((s) => s.id), ["s-local", "s-backend"]);
  const localMerged = merged.find((s) => s.id === "s-local")!;
  assert.equal(localMerged.source, "both");
  assert.equal(localMerged.turns, 5);
  assert.equal(localMerged.updatedAt, 1_700_000_100_000);
  const backendOnly = merged.find((s) => s.id === "s-backend")!;
  assert.equal(backendOnly.source, "backend");
  assert.equal(backendOnly.turns, 2);
  assert.equal(backendOnly.title, "这是我最近的回复");
  assert.deepEqual(backendOnly.messages, []);
});

test("sessionHistory：后端没有 preview / 时间时用兜底标题和当前时间", () => {
  const now = 5_000;
  const merged = mergeBackendSessions([], [{ session_id: "s-1" }], "未命名会话", now);
  assert.equal(merged[0].title, "未命名会话");
  assert.equal(merged[0].updatedAt, now);
  assert.equal(merged[0].source, "backend");
});

test("sessionHistory：normalizeThreadMessages 只认 user/assistant 且有文本", () => {
  const fromObject = normalizeThreadMessages({
    source: "checkpointer",
    messages: [
      { role: "human", content: "问题" },
      { role: "ai", content: "回答" },
      { role: "tool", content: "工具输出" },
      { role: "system", content: "系统" },
      { role: "assistant", content: "   " },
      { role: "assistant", content: 123 },
      "not-a-message",
    ],
  });
  assert.deepEqual(fromObject, [
    { id: "thread-0", role: "user", content: "问题" },
    { id: "thread-1", role: "assistant", content: "回答" },
  ]);
  assert.deepEqual(normalizeThreadMessages(null), []);
  assert.deepEqual(normalizeThreadMessages({ messages: "bad" }), []);
  // 也接受裸数组。
  assert.equal(normalizeThreadMessages([{ role: "user", content: "x" }]).length, 1);
});

test("sessionHistory：mergeHistory 后端不少于本地时优先后端，否则保留更完整的本地", () => {
  const local = [{ id: "l1", role: "user" as const, content: "本地" }];
  const backend = [
    { id: "b1", role: "user" as const, content: "后端问题" },
    { id: "b2", role: "assistant" as const, content: "后端回答" },
  ];
  assert.deepEqual(mergeHistory(local, backend), backend);
  assert.deepEqual(mergeHistory(local, []), local);
  // 本地有 3 条（含语义缓存轮次）、后端只有 2 条：不拿不完整的后端覆盖本地。
  const longerLocal = [
    ...local,
    { id: "l2", role: "assistant" as const, content: "缓存回答" },
    { id: "l3", role: "user" as const, content: "再来一轮" },
  ];
  assert.deepEqual(mergeHistory(longerLocal, backend), longerLocal);
});

test("sessionHistory：newSessionId 可注入随机源，稳定可测", () => {
  assert.equal(newSessionId(() => 0.5), newSessionId(() => 0.5));
  assert.ok(newSessionId().startsWith("s-"));
});
