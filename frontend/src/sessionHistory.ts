/**
 * P10-4 会话历史的前端纯函数层。
 *
 * 数据策略（与 brief 一致：**后端 thread 查询优先，localStorage 兜底**）：
 *   1. localStorage 保存最近若干会话的展示记录，保证后端不可用时仍能恢复历史；
 *   2. 页面加载时用后端 `GET /commerce/sessions` 的会话列表合并/补全本地列表；
 *   3. 点开某个会话时，优先用后端 `GET /commerce/sessions/{id}/history`（LangGraph
 *      checkpointer 真实消息）覆盖本地消息；后端失败或为空时保留本地消息。
 *
 * 本文件不依赖 React / DOM / fetch，全部是纯函数，便于 Node 原生 test runner 单测。
 */

export const SESSION_STORE_KEY = "ecommerce-agent.sessions.v1";

/** 本地最多保留的会话数（按 updatedAt 倒序）。 */
export const MAX_STORED_SESSIONS = 20;

/** 单个会话最多保留的消息条数（保留最新的）。 */
export const MAX_SESSION_MESSAGES = 100;

export type MessageRole = "user" | "assistant";

export interface StoredMessage {
  id: string;
  role: MessageRole;
  content: string;
}

export interface StoredSession {
  id: string;
  title: string;
  updatedAt: number;
  messages: StoredMessage[];
  /** local=仅本地；backend=后端列出；both=后端也有。 */
  source?: "local" | "backend" | "both";
  /** 后端给出的轮数（本地记录没有时使用）。 */
  turns?: number;
}

/** 后端 `GET /commerce/sessions` 的单项（允许缺字段，前端不猜值）。 */
export interface BackendSessionSummary {
  session_id: string;
  turns?: number;
  updated_at?: number;
  has_history?: boolean;
  preview?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}

function readNumber(record: Record<string, unknown>, key: string): number | undefined {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** 把后端时间戳（秒或毫秒）统一成毫秒。 */
export function toEpochMs(value: number | undefined, fallback = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return fallback;
  return value > 1e12 ? value : value * 1000;
}

/** 用首条用户提问当会话标题；空提问回退到给定标题，并截断到 max 字。 */
export function titleFromQuery(query: string, fallback: string, max = 24): string {
  const text = (query ?? "").replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** 归一化单条存储消息；role 非法或内容为空返回 null。 */
export function normalizeStoredMessage(value: unknown, index: number): StoredMessage | null {
  if (!isRecord(value)) return null;
  const role = readString(value, "role");
  if (role !== "user" && role !== "assistant") return null;
  const content = readString(value, "content");
  if (!content.trim()) return null;
  const id = readString(value, "id") || `m-${index}`;
  return { id, role, content };
}

/** 解析 localStorage 原始串；坏 JSON / 坏条目直接丢弃，绝不抛错。 */
export function parseSessionStore(raw: string | null): StoredSession[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const sessions: StoredSession[] = [];
  for (const item of parsed) {
    if (!isRecord(item)) continue;
    const id = readString(item, "id");
    if (!id) continue;
    const messages: StoredMessage[] = [];
    if (Array.isArray(item.messages)) {
      item.messages.forEach((message, index) => {
        const normalized = normalizeStoredMessage(message, index);
        if (normalized) messages.push(normalized);
      });
    }
    const title = readString(item, "title") || "";
    const source = readString(item, "source");
    const turns = readNumber(item, "turns");
    sessions.push({
      id,
      title,
      updatedAt: toEpochMs(readNumber(item, "updatedAt"), 0),
      messages: messages.slice(-MAX_SESSION_MESSAGES),
      ...(source === "local" || source === "backend" || source === "both" ? { source } : {}),
      ...(turns !== undefined ? { turns } : {}),
    });
  }
  return sortSessions(sessions).slice(0, MAX_STORED_SESSIONS);
}

/** 序列化会话列表（超量时按 updatedAt 倒序截断）。 */
export function serializeSessionStore(sessions: StoredSession[]): string {
  return JSON.stringify(sortSessions(sessions).slice(0, MAX_STORED_SESSIONS));
}

/** 按 updatedAt 倒序排列（不改原数组）。 */
export function sortSessions(sessions: StoredSession[]): StoredSession[] {
  return [...sessions].sort((a, b) => b.updatedAt - a.updatedAt);
}

/** 新增或更新一个会话（同 id 覆盖），并按 updatedAt 倒序 + 截断。 */
export function upsertSession(sessions: StoredSession[], session: StoredSession): StoredSession[] {
  const existing = sessions.find((item) => item.id === session.id);
  const merged: StoredSession = existing
    ? {
        ...existing,
        ...session,
        // 后端标记不因本地写入而丢失。
        source:
          existing.source === "backend" || existing.source === "both"
            ? "both"
            : session.source ?? existing.source ?? "local",
      }
    : { source: "local", ...session };
  const without = sessions.filter((item) => item.id !== session.id);
  return sortSessions([...without, merged]).slice(0, MAX_STORED_SESSIONS);
}

/** 删除会话；返回新数组。 */
export function removeSession(sessions: StoredSession[], id: string): StoredSession[] {
  return sessions.filter((item) => item.id !== id);
}

/**
 * 合并后端会话列表（后端优先补全，但不丢本地已保存的消息）。
 *
 * - 后端有、本地无：新建一条只有元数据的会话，标题用后端 preview / fallbackTitle；
 * - 两边都有：后端 turns / updatedAt 覆盖本地元数据，本地消息保留，source 记为 both；
 * - 本地有、后端无：保留本地记录（后端可能只列 trace 覆盖不到的缓存会话）。
 *
 * @param local 本地会话列表。
 * @param backend 后端 `/commerce/sessions` 返回的列表。
 * @param fallbackTitle 后端没有 preview 时的标题。
 * @param now 当前时间（毫秒），用于后端没有 updated_at 时兜底排序。
 * @returns 合并后的列表。
 */
export function mergeBackendSessions(
  local: StoredSession[],
  backend: BackendSessionSummary[],
  fallbackTitle: string,
  now = Date.now(),
): StoredSession[] {
  const merged = [...local];
  for (const raw of backend ?? []) {
    if (!raw || typeof raw.session_id !== "string" || !raw.session_id) continue;
    const id = raw.session_id;
    const index = merged.findIndex((item) => item.id === id);
    const backendTime = toEpochMs(raw.updated_at, now);
    const preview = typeof raw.preview === "string" ? raw.preview.replace(/\s+/g, " ").trim() : "";
    if (index >= 0) {
      const existing = merged[index];
      merged[index] = {
        ...existing,
        updatedAt: Math.max(existing.updatedAt, backendTime),
        source: "both",
        ...(raw.turns !== undefined ? { turns: raw.turns } : {}),
        ...(existing.title ? {} : { title: preview || fallbackTitle }),
      };
    } else {
      merged.push({
        id,
        title: preview ? (preview.length > 24 ? `${preview.slice(0, 24)}…` : preview) : fallbackTitle,
        updatedAt: backendTime,
        messages: [],
        source: "backend",
        ...(raw.turns !== undefined ? { turns: raw.turns } : {}),
      });
    }
  }
  return sortSessions(merged).slice(0, MAX_STORED_SESSIONS);
}

/**
 * 归一化后端 thread 历史消息。
 *
 * 接受 `{messages:[...]}` 或消息数组；只认 user/human 与 assistant/ai 两种角色，
 * 其它（tool / system）直接丢弃——前端不展示内部工具消息。
 *
 * @param raw 后端响应或消息数组。
 * @returns 最新在后的消息列表。
 */
export function normalizeThreadMessages(raw: unknown): StoredMessage[] {
  const list = Array.isArray(raw) ? raw : isRecord(raw) && Array.isArray(raw.messages) ? raw.messages : [];
  const messages: StoredMessage[] = [];
  list.forEach((item, index) => {
    if (!isRecord(item)) return;
    const roleRaw = readString(item, "role").toLowerCase();
    const role: MessageRole | null =
      roleRaw === "user" || roleRaw === "human"
        ? "user"
        : roleRaw === "assistant" || roleRaw === "ai"
          ? "assistant"
          : null;
    if (!role) return;
    const content = readString(item, "content");
    if (!content.trim()) return;
    messages.push({ id: `thread-${index}`, role, content });
  });
  return messages.slice(-MAX_SESSION_MESSAGES);
}

/**
 * 后端 thread 历史优先：后端消息**不少于**本地时整体采用（checkpointer 是真实记录），
 * 否则保留更完整的本地展示记录（语义缓存命中的轮次不会写进 checkpointer）。
 *
 * @param local 本地消息。
 * @param backend 后端 thread 消息。
 * @returns 合并后的消息列表。
 */
export function mergeHistory(local: StoredMessage[], backend: StoredMessage[]): StoredMessage[] {
  return backend.length > 0 && backend.length >= local.length ? backend : local;
}

/** 生成新会话 id（可注入随机源，便于测试）。 */
export function newSessionId(random: () => number = Math.random): string {
  return `s-${random().toString(36).slice(2, 10)}`;
}

/** 会话 id 的短展示串。 */
export function shortSessionId(id: string, length = 10): string {
  if (!id) return "";
  return id.length <= length ? id : `${id.slice(0, length)}…`;
}
