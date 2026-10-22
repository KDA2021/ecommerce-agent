import { detectSubagentFailure } from "../api";
import { PREVIEW_STATE } from "../orderPreview";
import type { StoredMessage } from "../sessionHistory";
import type { ChatMessage, JsonRecord } from "./types";

/** 本地生成的展示用消息 id（crypto 不可用时退化成时间戳 + 随机后缀）。 */
export function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `m-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** 当前前端路由路径（尾部斜杠归一化，空路径退化成 "/"）。 */
export function currentPath(): string {
  const raw = window.location.pathname.replace(/\/+$/, "");
  return raw || "/";
}

export function readStorage(key: string): string | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(key, value);
  } catch {
    // 隐私模式 / 配额满：历史记录能力降级，不影响对话。
  }
}

export function asRecord(value: unknown): JsonRecord | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as JsonRecord;
}

export function readString(record: JsonRecord | null, key: string): string | undefined {
  const value = record?.[key];
  return typeof value === "string" ? value : undefined;
}

export function readNumber(record: JsonRecord | null, key: string): number | undefined {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** 展示用消息 → 可持久化消息（只有文本，转存 SSE/订单等临时状态没意义）。 */
export function toStoredMessages(messages: ChatMessage[]): StoredMessage[] {
  return messages
    .filter((message) => message.content.trim().length > 0)
    .map((message) => ({ id: message.id, role: message.role, content: message.content }));
}

/** 本地存储消息 → 展示消息（恢复的历史没有 trace / 订单等临时状态）。 */
export function toChatMessages(messages: StoredMessage[]): ChatMessage[] {
  return messages.map((message) => {
    // P12-3：后端 thread 里的 AIMessage 可能含失败标记，恢复历史时照样挂错误卡片。
    const failure = message.role === "assistant" ? detectSubagentFailure(message.content) : null;
    return {
      id: message.id,
      role: message.role,
      content: message.content,
      ...(message.role === "assistant" ? { feedbackStatus: "idle" as const } : {}),
      ...(failure?.failed ? { subagentError: true, subagentDetail: failure.detail } : {}),
    };
  });
}

/** 预览面板状态 → i18n key。 */
export function previewHintKey(state: string): string {
  if (state === PREVIEW_STATE.CONFIRMED) return "order.confirmedHint";
  if (state === PREVIEW_STATE.CANCELLED) return "order.cancelledHint";
  return "order.pendingHint";
}
