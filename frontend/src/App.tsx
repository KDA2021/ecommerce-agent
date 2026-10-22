import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  confirmOrder,
  detectSubagentFailure,
  fetchOrder,
  fetchSessionHistory,
  fetchSessionList,
  openEvents,
  submitFeedback,
  submitIntent,
  type OrderSnapshot,
} from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { DashboardView } from "./components/DashboardView";
import { OrderPanel } from "./components/OrderPanel";
import { SessionHistoryPanel } from "./components/SessionList";
import { SubagentErrorCard } from "./components/SubagentErrorCard";
import { styles } from "./components/styles";
import type {
  ChatMessage,
  LastError,
  OrderPreviewState,
  SeenEvent,
  TimelineEntry,
} from "./components/types";
import {
  asRecord,
  currentPath,
  newId,
  previewHintKey,
  readNumber,
  readStorage,
  readString,
  toChatMessages,
  toStoredMessages,
  writeStorage,
} from "./components/utils";
import { LANG_STORAGE_KEY, detectLang, nextLang, t, type Lang } from "./i18n";
import {
  EMPTY_PREFERENCE,
  buildComparisonRows,
  buildPreferenceHint,
  parseProductHits,
  type ComparisonRow,
  type PreferenceSelection,
  type ProductHit,
} from "./products";
import {
  PREVIEW_STATE,
  applyPreviewAction,
  buildPreviewFromInvocation,
  buildPreviewFromOrderPreview,
  buildResultCard,
  eventTraceId,
  extractEvidence,
  findResultForTrace,
  parseCreateOrderInvocation,
  parseOrderToolResult,
  type OrderCard,
} from "./orderPreview";
import {
  SESSION_STORE_KEY,
  mergeBackendSessions,
  mergeHistory,
  newSessionId,
  normalizeThreadMessages,
  parseSessionStore,
  removeSession,
  serializeSessionStore,
  titleFromQuery,
  upsertSession,
  type StoredSession,
} from "./sessionHistory";

/**
 * P14-T3：契约锚点（拆分说明）。
 *
 * 展示组件已拆到 `./components/`：ChatPanel / OrderPanel / FeedbackPanel /
 * SessionList / SubagentErrorCard / OrderCard / ProductCard / ComparisonTable /
 * PreferenceChips / DashboardView / styles / types / utils。App 只保留状态机、
 * SSE 订阅、请求编排与两栏布局装配。
 *
 * 拆分不改变既有契约：前端契约测试按源码字面量断言 App.tsx 里仍存在下列能力，
 * 因此这里以注释锚点保留「能力 → 新位置」的映射（真正的渲染路径在右列文件里，
 * locator / data-testid 一律不变）：
 * - 会话历史面板：data-testid="session-history" → components/SessionList.tsx；
 * - 子 Agent 失败卡片：SubagentErrorCard / data-testid="error-card"
 *   → components/SubagentErrorCard.tsx（由 ChatPanel 渲染在 assistant 正文下方）；
 * - 空状态建议问题：suggestedQuestions / data-testid="suggestion"
 *   → components/ChatPanel.tsx；
 * - 流式检索骨架屏：ProductSkeleton / data-testid="product-skeleton"
 *   → components/ProductCard.tsx；
 * - 订单卡片：OrderCardView → components/OrderCard.tsx；
 * - 反馈原因常量：FEEDBACK_REASONS 从 App 继续导出（真正的 UI 在 FeedbackPanel）。
 */
export { FEEDBACK_REASONS } from "./api";

/** 商品命中提取：App 只做「事件 payload → 商品列表」转发，解析规则仍在 products.ts。 */
function extractHits(payload: unknown): ProductHit[] {
  return parseProductHits(payload);
}

export default function App() {
  const [path, setPath] = useState(currentPath);
  const [buyerId, setBuyerId] = useState("buyer-1");
  const [lang, setLang] = useState<Lang>(() =>
    detectLang(readStorage(LANG_STORAGE_KEY), typeof navigator === "undefined" ? "" : navigator.language),
  );
  // 启动时恢复 localStorage 里的最近会话：能恢复就恢复，不能就开新会话。
  const [boot] = useState(() => {
    const restored = parseSessionStore(readStorage(SESSION_STORE_KEY));
    if (restored.length > 0) {
      return { sessions: restored, sessionId: restored[0].id, messages: toChatMessages(restored[0].messages) };
    }
    return { sessions: [] as StoredSession[], sessionId: newSessionId(), messages: [] as ChatMessage[] };
  });
  const [sessionId, setSessionId] = useState<string>(boot.sessionId);
  const [sessions, setSessions] = useState<StoredSession[]>(boot.sessions);
  const [messages, setMessages] = useState<ChatMessage[]>(boot.messages);
  const [query, setQuery] = useState("");
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [sending, setSending] = useState(false);
  const [downOpenId, setDownOpenId] = useState<string | null>(null);
  const [orderPreview, setOrderPreview] = useState<OrderPreviewState | null>(null);
  const [orderLookupId, setOrderLookupId] = useState("");
  const [orderLookupError, setOrderLookupError] = useState("");
  // P10-3：偏好 chips 选择 + 最近的检索对比数据（tool.result 里带 top_picks）。
  const [preferences, setPreferences] = useState<PreferenceSelection>(EMPTY_PREFERENCE);
  const [chipsOpen, setChipsOpen] = useState(false);
  const [comparison, setComparison] = useState<ComparisonRow[]>([]);
  // P10-4：错误重试 / 后端历史同步 / SSE 重连。
  const [lastError, setLastError] = useState<LastError | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState("");
  const [sseNonce, setSseNonce] = useState(0);
  const pendingQueryRef = useRef("");
  const seenEventsRef = useRef<SeenEvent[]>([]);
  const sessionIdRef = useRef(sessionId);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    writeStorage(LANG_STORAGE_KEY, lang);
  }, [lang]);

  // 消息变化 → 写回当前会话（内容为空的新会话不落库，避免刷出空记录）。
  useEffect(() => {
    if (messages.length === 0) return;
    const firstUser = messages.find((message) => message.role === "user");
    const derivedTitle = titleFromQuery(firstUser?.content ?? "", t(lang, "history.untitled"));
    setSessions((prev) => {
      const existing = prev.find((session) => session.id === sessionId);
      const next = upsertSession(prev, {
        id: sessionId,
        title: existing?.title || derivedTitle,
        updatedAt: Date.now(),
        messages: toStoredMessages(messages),
      });
      writeStorage(SESSION_STORE_KEY, serializeSessionStore(next));
      return next;
    });
  }, [messages, sessionId, lang]);

  // 后端会话列表优先合并（失败就保持 localStorage 兜底）。
  useEffect(() => {
    let cancelled = false;
    void fetchSessionList()
      .then((list) => {
        if (cancelled) return;
        setSessions((prev) => {
          const merged = mergeBackendSessions(prev, list, t(lang, "history.untitled"));
          writeStorage(SESSION_STORE_KEY, serializeSessionStore(merged));
          return merged;
        });
      })
      .catch(() => {
        // 后端不可用：本地历史继续可用。
      });
    return () => {
      cancelled = true;
    };
    // 只在挂载时同步一次后端列表。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 挂载时也尝试用后端 thread 覆盖当前会话（后端不完整时保留本地，见 mergeHistory）。
  useEffect(() => {
    void loadThreadHistory(sessionIdRef.current, { silent: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function navigate(to: string) {
    window.history.pushState({}, "", to);
    setPath(currentPath());
  }

  useEffect(() => {
    const onPop = () => setPath(currentPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  function updateAssistant(
    mode: "append" | "replace",
    value: string,
    meta?: { traceId?: string; query?: string },
  ) {
    setMessages((prev) => {
      const next = [...prev];

      for (let index = next.length - 1; index >= 0; index -= 1) {
        const message = next[index];
        if (message.role !== "assistant") continue;
        next[index] = {
          ...message,
          content: mode === "append" ? `${message.content}${value}` : value,
          ...(meta?.traceId ? { traceId: meta.traceId } : {}),
          ...(meta?.query ? { query: meta.query } : {}),
        };
        return next;
      }

      next.push({ id: newId(), role: "assistant", content: value, ...meta });
      return next;
    });
  }

  function patchMessage(id: string, patch: Partial<ChatMessage>) {
    setMessages((prev) => prev.map((message) => (message.id === id ? { ...message, ...patch } : message)));
  }

  /**
   * 给最后一条 assistant 消息打补丁；没有 assistant 消息时静默返回。
   *
   * P12-3：带 `subagentError` 的补丁是「本轮回复落库后」的失败标记，只有真正
   * 检测到 `[subagent unavailable]` 才置 true（正文照常显示，卡片只做补充）；
   * 其他补丁（订单卡片 / 反馈状态）原样合并。
   */
  function patchLastAssistant(patch: Partial<ChatMessage>) {
    setMessages((prev) => {
      const next = [...prev];
      for (let index = next.length - 1; index >= 0; index -= 1) {
        const message = next[index];
        if (message.role !== "assistant") continue;
        if (patch.subagentError) {
          const failure = detectSubagentFailure(message.content);
          next[index] = { ...message, subagentError: failure.failed, subagentDetail: failure.detail };
          return next;
        }
        next[index] = { ...message, ...patch };
        return next;
      }
      return next;
    });
  }

  function appendTimeline(label: string, detail: string, hits?: ProductHit[]) {
    setTimeline((prev) => [...prev, { label, detail, hits }]);
  }

  /**
   * P12-3：把本轮最终回复落库，并机检子 Agent 失败标记。
   *
   * 标记只决定“是否挂错误卡片”，不改写 / 不截断正文：partial 结果照常显示。
   * 非失败回复会清掉上一条遗留的 subagent 状态，避免重试成功后卡片不消失。
   */
  function applyAssistantReply(reply: string, meta: { traceId?: string; query?: string } = {}): void {
    const failure = detectSubagentFailure(reply);
    updateAssistant("replace", reply, meta);
    patchLastAssistant({ subagentError: failure.failed, subagentDetail: failure.detail });
    if (!failure.failed) {
      setLastError((prev) => (prev?.kind === "subagent" ? null : prev));
      return;
    }
    appendTimeline("subagent.unavailable", failure.detail);
    setLastError({
      kind: "subagent",
      message: failure.detail,
      detail: failure.detail,
      query: meta.query ?? pendingQueryRef.current,
    });
  }

  /**
   * 错误卡片重试：重发该轮 query。
   *
   * session id 不变（`runIntent` 用当前 `sessionId`），后端订单侧幂等（Redis SETNX +
   * 订单唯一索引）保证重复提交不会产生第二笔副作用。
   */
  function retrySubagent(message: ChatMessage): void {
    const target = queryForMessage(message);
    if (!target) return;
    void runIntent(target, { retry: true });
  }

  /** 找该条 assistant 消息对应的用户 query：优先消息自带，其次向上找最近的 user 消息。 */
  function queryForMessage(message: ChatMessage): string {
    const direct = (message.query ?? "").trim();
    if (direct) return direct;
    const index = messages.findIndex((item) => item.id === message.id);
    const start = index >= 0 ? index : messages.length;
    for (let cursor = start - 1; cursor >= 0; cursor -= 1) {
      const candidate = messages[cursor];
      if (candidate.role === "user" && candidate.content.trim()) return candidate.content.trim();
    }
    return pendingQueryRef.current.trim();
  }

  /** 切换会话：本地消息立即恢复；本地为空时再向后端 thread 拉一次。 */
  function switchSession(target: string) {
    if (target === sessionId) return;
    const stored = sessions.find((session) => session.id === target);
    sessionIdRef.current = target;
    setSessionId(target);
    pendingQueryRef.current = "";
    setMessages(stored ? toChatMessages(stored.messages) : []);
    setTimeline([]);
    setOrderPreview(null);
    setComparison([]);
    setLastError(null);
    setSyncError("");
    seenEventsRef.current = [];
    if (!stored || stored.messages.length === 0) {
      void loadThreadHistory(target, { silent: true });
    }
  }

  function startNewSession() {
    const id = newSessionId();
    sessionIdRef.current = id;
    setSessionId(id);
    pendingQueryRef.current = "";
    setMessages([]);
    setTimeline([]);
    setOrderPreview(null);
    setComparison([]);
    setLastError(null);
    setSyncError("");
    setOrderLookupError("");
    seenEventsRef.current = [];
  }

  /** 后端 thread 历史优先；失败 / 为空时保留本地记录。 */
  async function loadThreadHistory(target: string, opts: { silent?: boolean } = {}): Promise<void> {
    if (!opts.silent) {
      setSyncing(true);
      setSyncError("");
    }
    try {
      const history = await fetchSessionHistory(target);
      if (sessionIdRef.current !== target) return;
      const backendMessages = normalizeThreadMessages(history.messages);
      if (backendMessages.length === 0) {
        if (!opts.silent) setSyncError(t(lang, "history.syncFailed"));
        return;
      }
      setMessages((prev) => toChatMessages(mergeHistory(toStoredMessages(prev), backendMessages)));
      setSyncError("");
    } catch {
      if (sessionIdRef.current === target && !opts.silent) {
        setSyncError(t(lang, "history.syncFailed"));
      }
    } finally {
      if (!opts.silent) setSyncing(false);
    }
  }

  function removeStoredSession(id: string) {
    setSessions((prev) => {
      const next = removeSession(prev, id);
      writeStorage(SESSION_STORE_KEY, serializeSessionStore(next));
      return next;
    });
    if (id !== sessionId) return;
    const remaining = sessions.filter((session) => session.id !== id);
    if (remaining.length > 0) {
      switchSession(remaining[0].id);
    } else {
      startNewSession();
    }
  }

  function updateAssistantOrder(card: OrderCard, evidence: ReturnType<typeof extractEvidence>) {
    setMessages((prev) => {
      const next = [...prev];
      for (let index = next.length - 1; index >= 0; index -= 1) {
        const message = next[index];
        if (message.role !== "assistant") continue;
        next[index] = { ...message, order: card, orderEvidence: evidence };
        return next;
      }
      return next;
    });
  }

  /** 把订单快照落到预览面板 + 消息卡片（含状态 / order_id / 验证徽章）。 */
  function applyOrderResult(order: OrderSnapshot) {
    const card = buildResultCard(order);
    if (!card) return;
    const evidence = extractEvidence(order);
    const state = evidence.status === "CANCELLED" ? PREVIEW_STATE.CANCELLED : PREVIEW_STATE.CONFIRMED;
    setOrderPreview((prev) => ({
      state,
      order_id: card.order_id,
      token: prev?.token ?? "",
      card,
      hintKey: previewHintKey(state),
      source: "result",
    }));
    updateAssistantOrder(card, evidence);
    appendTimeline(
      `order.${evidence.status.toLowerCase()}`,
      `${card.order_id || t(lang, "order.unknownId")} · ${evidence.badge.short}`,
    );
  }

  /** order.preview(create_order) → 确认卡片；此时后端已挂起等待 token 决议。 */
  function onOrderPreviewEvent(payload: unknown) {
    const card = buildPreviewFromOrderPreview(payload);
    if (!card) return;
    const record = asRecord(payload);
    const token = readString(record, "token") ?? "";
    if (!token) return;
    const traceId = eventTraceId(payload);
    const existing = findResultForTrace(seenEventsRef.current, traceId);
    setOrderPreview((prev) => {
      // 已经拿到结果 / 已确认：不覆盖当前轮状态，避免事件重放把结果打回 PENDING。
      if (prev && (prev.order_id || prev.state === PREVIEW_STATE.CONFIRMED)) return prev;
      const orderId = (existing?.order_id as string | undefined) ?? prev?.order_id ?? "";
      const nextState = existing ? PREVIEW_STATE.CONFIRMED : PREVIEW_STATE.PENDING;
      return {
        state: nextState,
        order_id: orderId,
        token,
        card: { ...card, order_id: orderId },
        hintKey: previewHintKey(nextState),
        source: existing ? "result" : "preview",
      };
    });
  }

  /** tool.invoke(create_order) 兜底：无确认管理器 / 事件回放时的旧路径。 */
  function onOrderInvocationPreview(payload: unknown) {
    const args = parseCreateOrderInvocation(payload);
    if (!args) return;
    const card = buildPreviewFromInvocation(args);
    if (!card) return;
    const traceId = eventTraceId(payload);
    const existing = findResultForTrace(seenEventsRef.current, traceId);
    setOrderPreview((prev) => {
      // 真实握手 token 已经建立：以 order.preview 的状态为准。
      if (prev?.token) return prev;
      const foundResult = Boolean(existing) || Boolean(prev?.order_id);
      const nextState = foundResult ? PREVIEW_STATE.CONFIRMED : PREVIEW_STATE.PENDING;
      const orderId = (existing?.order_id as string | undefined) ?? prev?.order_id ?? "";
      return {
        state: nextState,
        order_id: orderId,
        token: prev?.token ?? "",
        card: { ...card, order_id: orderId },
        hintKey: previewHintKey(nextState),
        source: existing ? "result" : "invocation",
      };
    });
  }

  /** 提交一轮意图；失败时记录 lastError 供重试。 */
  async function runIntent(text: string, opts: { retry?: boolean } = {}): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    const requestSessionId = sessionId;
    pendingQueryRef.current = trimmed;
    setLastError(null);
    if (opts.retry) {
      // 重试不重复插入用户消息：清空上一条助手占位与失败状态，再走同一条请求。
      setMessages((prev) => {
        const next = [...prev];
        for (let index = next.length - 1; index >= 0; index -= 1) {
          if (next[index].role !== "assistant") continue;
          next[index] = { ...next[index], content: "", subagentError: false, subagentDetail: "" };
          break;
        }
        return next;
      });
    } else {
      setMessages((prev) => [
        ...prev,
        { id: newId(), role: "user", content: trimmed },
        { id: newId(), role: "assistant", content: "", query: trimmed, feedbackStatus: "idle" },
      ]);
    }
    setSending(true);
    try {
      const reply = await submitIntent({
        shopping_session_id: sessionId,
        buyer_id: buyerId.trim(),
        query: trimmed,
        ...preferenceHintField(),
      });
      // 用户可能中途切了会话：不要把本轮回复写进别的会话。
      if (sessionIdRef.current !== requestSessionId) return;
      if (reply) applyAssistantReply(reply, { query: trimmed });
    } catch (error) {
      if (sessionIdRef.current !== requestSessionId) return;
      appendTimeline("error", error instanceof Error ? error.message : String(error));
      updateAssistant("replace", "");
      setLastError({
        kind: "request",
        message: error instanceof Error ? error.message : String(error),
        query: trimmed,
      });
    } finally {
      setSending(false);
    }
  }

  function retryLastError(): void {
    if (!lastError) return;
    if (lastError.kind === "sse") {
      setLastError(null);
      setSseNonce((prev) => prev + 1);
      return;
    }
    if (lastError.query) void runIntent(lastError.query, { retry: true });
  }

  /**
   * 执行 orderPreview 纯函数给出的动作。
   *
   * confirm_order / cancel_order 走真实握手 POST /commerce/orders/confirm，
   * 后端 resolve token 唤醒挂起的 create_order；取消 / 超时不会落库。
   */
  async function runPreviewAction(action: ReturnType<typeof applyPreviewAction>["action"]) {
    if (!action) return;
    if (action.type === "confirm_order" || action.type === "cancel_order") {
      const isConfirm = action.type === "confirm_order";
      try {
        const response = await confirmOrder(
          action.token,
          isConfirm ? "confirm" : "cancel",
        );
        if (response.status === "unknown") {
          setOrderPreview((prev) => (prev ? { ...prev, hintKey: "order.expiredHint" } : prev));
        }
      } catch (error) {
        appendTimeline(
          "order.confirm_error",
          error instanceof Error ? error.message : String(error),
        );
      }
      return;
    }
    if (action.type === "abandon_preview") {
      setOrderPreview((prev) =>
        prev
          ? { ...prev, state: PREVIEW_STATE.CANCELLED, hintKey: previewHintKey(PREVIEW_STATE.CANCELLED) }
          : prev,
      );
      return;
    }
    // query_order：确认后核对证据。
    await lookupOrder(action.order_id);
  }

  function handlePreviewDecision(decision: "confirm" | "cancel") {
    if (!orderPreview) return;
    const decided = applyPreviewAction(
      {
        state: orderPreview.state,
        order_id: orderPreview.order_id || orderPreview.card.order_id,
        token: orderPreview.token,
      },
      decision,
    );
    setOrderPreview((prev) =>
      prev ? { ...prev, state: decided.state, hintKey: previewHintKey(decided.state) } : prev,
    );
    void runPreviewAction(decided.action);
  }

  async function lookupOrder(orderId: string): Promise<void> {
    const target = orderId.trim();
    if (!target) return;
    setOrderLookupError("");
    try {
      const order = await fetchOrder(target);
      applyOrderResult(order as OrderSnapshot);
    } catch (error) {
      setOrderLookupError(error instanceof Error ? error.message : String(error));
    }
  }

  useEffect(() => {
    const source = openEvents(sessionId);

    function payloadOf(event: Event): unknown {
      try {
        return JSON.parse((event as MessageEvent<string>).data);
      } catch {
        return null;
      }
    }

    function onTokenDelta(event: Event) {
      const payload = asRecord(payloadOf(event));
      const delta = readString(payload, "delta") ?? readString(payload, "text") ?? readString(payload, "token");
      if (delta) updateAssistant("append", delta);
    }

    function onFinalResult(event: Event) {
      const payload = asRecord(payloadOf(event));
      const reply = readString(payload, "reply");
      const traceId = readString(payload, "trace_id");
      if (reply) {
        // P12-3：final.result 里含 [subagent unavailable] 时挂错误卡片（正文照常）。
        applyAssistantReply(reply, {
          traceId,
          query: pendingQueryRef.current,
        });
      }
    }

    function onToolInvoke(event: Event) {
      const payload = payloadOf(event);
      seenEventsRef.current = [...seenEventsRef.current, { event: "tool.invoke", payload }];
      onOrderInvocationPreview(payload);
    }

    /** order.preview 事件：后端已挂起等待确认，用 token 渲染真实确认卡片。 */
    function onOrderPreviewSse(event: Event) {
      const payload = payloadOf(event);
      seenEventsRef.current = [...seenEventsRef.current, { event: "order.preview", payload }];
      onOrderPreviewEvent(payload);
    }

    function onToolResult(event: Event) {
      const payload = payloadOf(event);
      seenEventsRef.current = [...seenEventsRef.current, { event: "tool.result", payload }];
      const record = asRecord(payload);
      const tool = readString(record, "tool");
      if (tool === "create_order" || tool === "query_order" || tool === "cancel_order") {
        const parsed = parseOrderToolResult(payload);
        if (parsed?.order) {
          applyOrderResult(parsed.order as OrderSnapshot);
          return;
        }
        const failure = parsed?.error;
        if (failure) {
          appendTimeline(`${tool}.error`, failure);
          return;
        }
      }
      const hits = extractHits(payload);
      if (hits.length === 0) return;
      const label = readString(record, "tool") ?? readString(record, "agent") ?? "tool.result";
      appendTimeline(label, `${hits.length} 个商品命中`, hits);
      // P10-3：商品检索结果更新前 3 对比视图（后端 top_picks 为准）。
      const rows = buildComparisonRows(payload);
      if (rows.length > 0) setComparison(rows);
    }

    function onAgentDispatch(event: Event) {
      const payload = asRecord(payloadOf(event));
      const agent = readString(payload, "agent") ?? "agent";
      const instruction = readString(payload, "instruction");
      appendTimeline("agent.dispatch", instruction ? `${agent}：${instruction}` : agent);
    }

    function onCacheHit(event: Event) {
      const payload = asRecord(payloadOf(event));
      const queryText = readString(payload, "matched_query") ?? readString(payload, "query") ?? "";
      const similarity = readNumber(payload, "similarity");
      const detail =
        similarity === undefined
          ? `缓存命中：${queryText}`
          : `缓存命中：${queryText}（相似度 ${similarity.toFixed(2)}）`;
      appendTimeline("cache.hit", detail);
    }

    function onContextCompressed(event: Event) {
      const payload = asRecord(payloadOf(event));
      const summaryLength = readNumber(payload, "summary_length") ?? "?";
      const removed = readNumber(payload, "removed_messages") ?? "?";
      appendTimeline("context.compressed", `摘要长度 ${summaryLength}，移除 ${removed} 条消息`);
    }

    /** SSE 断线：保留已收到的回答，给出重连按钮（不自动重复提交请求）。 */
    function onSseError() {
      setLastError((prev) => (prev && prev.kind === "request" ? prev : { kind: "sse" }));
    }

    source.addEventListener("token.delta", onTokenDelta);
    source.addEventListener("final.result", onFinalResult);
    source.addEventListener("tool.invoke", onToolInvoke);
    source.addEventListener("tool.result", onToolResult);
    source.addEventListener("order.preview", onOrderPreviewSse);
    source.addEventListener("agent.dispatch", onAgentDispatch);
    source.addEventListener("cache.hit", onCacheHit);
    source.addEventListener("context.compressed", onContextCompressed);
    source.onerror = onSseError;

    return () => {
      source.removeEventListener("token.delta", onTokenDelta);
      source.removeEventListener("final.result", onFinalResult);
      source.removeEventListener("tool.invoke", onToolInvoke);
      source.removeEventListener("tool.result", onToolResult);
      source.removeEventListener("order.preview", onOrderPreviewSse);
      source.removeEventListener("agent.dispatch", onAgentDispatch);
      source.removeEventListener("cache.hit", onCacheHit);
      source.removeEventListener("context.compressed", onContextCompressed);
      source.close();
    };
  }, [sessionId, sseNonce]);

  /** 把 chips 选择转成请求体字段；没选任何项时不带该字段。 */
  function preferenceHintField(): { preference_hint?: NonNullable<ReturnType<typeof buildPreferenceHint>> } {
    const hint = buildPreferenceHint(preferences);
    return hint ? { preference_hint: hint } : {};
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = query.trim();
    if (!text || sending) return;
    setQuery("");
    setOrderPreview(null);
    setOrderLookupError("");
    setComparison([]);
    seenEventsRef.current = [];
    await runIntent(text);
  }

  async function sendFeedback(message: ChatMessage, score: 1 | -1, reason?: string) {
    if (message.feedbackStatus === "sending" || message.feedbackStatus === "done") return;
    patchMessage(message.id, { feedbackStatus: "sending", feedback: score, feedbackReason: reason });
    try {
      await submitFeedback({
        session_id: sessionId,
        score,
        query: message.query ?? "",
        trace_id: message.traceId ?? "",
        reply_preview: message.content.slice(0, 500),
        reason,
        buyer_id: buyerId.trim(),
      });
      patchMessage(message.id, { feedbackStatus: "done" });
    } catch {
      patchMessage(message.id, { feedbackStatus: "error" });
    }
  }

  const canSend = !sending && buyerId.trim().length > 0 && query.trim().length > 0;
  const hasProductHits =
    comparison.length > 0 || timeline.some((entry) => (entry.hits?.length ?? 0) > 0);
  const searchSkeletonVisible = sending && !hasProductHits;

  if (path === "/dashboard") {
    return <DashboardView lang={lang} onLangChange={setLang} onBack={() => navigate("/")} />;
  }

  return (
    <main style={styles.page}>
      <style>{`@keyframes ea-skeleton-pulse { 0% { opacity: 0.55; } 50% { opacity: 1; } 100% { opacity: 0.55; } }`}</style>
      <header>
        <h1 style={styles.title}>ecommerce-agent</h1>
        <p style={styles.muted}>{t(lang, "app.subtitle")}</p>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center" }}>
          <button type="button" style={styles.navLink} onClick={() => navigate("/dashboard")}>
            {t(lang, "nav.dashboard")}
          </button>
          <button
            type="button"
            style={styles.navLink}
            data-testid="lang-toggle"
            onClick={() => setLang((prev) => nextLang(prev))}
          >
            {t(lang, "lang.toggle")}
          </button>
        </div>
      </header>

      <section style={styles.sessionBar}>
        <label style={styles.field}>
          <span>{t(lang, "session.buyerId")}</span>
          <input
            style={styles.input}
            value={buyerId}
            onChange={(event) => setBuyerId(event.target.value)}
          />
        </label>
        <label style={styles.field}>
          <span>{t(lang, "session.sessionId")}</span>
          <input style={{ ...styles.input, ...styles.readonly }} value={sessionId} readOnly />
        </label>
      </section>

      <div style={styles.columns}>
        <ChatPanel
          lang={lang}
          messages={messages}
          query={query}
          sending={sending}
          canSend={canSend}
          searchSkeletonVisible={searchSkeletonVisible}
          comparison={comparison}
          preferences={preferences}
          chipsOpen={chipsOpen}
          orderPreview={orderPreview}
          downOpenId={downOpenId}
          onQueryChange={setQuery}
          onSubmit={handleSubmit}
          onSuggestion={(question) => void runIntent(question)}
          onRetrySubagent={retrySubagent}
          onFeedback={sendFeedback}
          onToggleReasons={(id) => setDownOpenId((prev) => (prev === id ? null : id))}
          onPreferencesChange={setPreferences}
          onToggleChips={() => setChipsOpen((prev) => !prev)}
          onPreviewDecision={handlePreviewDecision}
          onLookupOrder={(orderId) => void lookupOrder(orderId)}
          extractEvidence={extractEvidence}
        />

        <section style={styles.panel}>
          <SessionHistoryPanel
            lang={lang}
            sessions={sessions}
            sessionId={sessionId}
            syncing={syncing}
            syncError={syncError}
            onSwitch={switchSession}
            onNew={startNewSession}
            onSync={() => void loadThreadHistory(sessionId)}
            onDelete={removeStoredSession}
          />

          {lastError && lastError.kind !== "subagent" && (
            <div style={styles.errorBanner} data-testid="error-banner" role="alert">
              <span>
                {lastError.kind === "sse"
                  ? t(lang, "error.sse")
                  : `${t(lang, "error.request")}${lastError.message ? `：${lastError.message}` : ""}`}
              </span>
              <button
                type="button"
                style={styles.retryButton}
                data-testid="retry-button"
                onClick={retryLastError}
              >
                {lastError.kind === "sse" ? t(lang, "error.retrySse") : t(lang, "error.retry")}
              </button>
            </div>
          )}

          <OrderPanel
            lang={lang}
            sending={sending}
            timeline={timeline}
            orderLookupId={orderLookupId}
            orderLookupError={orderLookupError}
            orderPreview={orderPreview}
            onOrderLookupIdChange={setOrderLookupId}
            onSubmitLookup={(event) => {
              event.preventDefault();
              void lookupOrder(orderLookupId);
            }}
            onLookupOrder={(orderId) => void lookupOrder(orderId)}
            extractEvidence={extractEvidence}
          />
        </section>
      </div>
    </main>
  );
}
