import type { OrderCard, PreviewState } from "../orderPreview";
import type { ProductHit } from "../products";

/** JSON 对象记录：SSE payload / 存储结构共用的宽松类型。 */
export type JsonRecord = Record<string, unknown>;

/**
 * 前端跨组件共享视图类型。
 *
 * 这些类型原本内联在 App.tsx：拆出组件后由消息列表 / 预览面板 / 时间线 /
 * 错误重试四处共同消费，集中放这里避免 App 与子组件互相 import 成环。
 */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  query?: string;
  traceId?: string;
  feedback?: 1 | -1;
  feedbackReason?: string;
  feedbackStatus?: "idle" | "sending" | "done" | "error";
  order?: OrderCard;
  orderEvidence?: ChatOrderEvidence;
  /** P12-3：本轮 final.result / AIMessage 含 `[subagent unavailable]`。 */
  subagentError?: boolean;
  /** P12-3：含标记的那一行文本，用于错误卡片说明失败原因。 */
  subagentDetail?: string;
}

/** 订单证据徽章：由 orderPreview.extractEvidence 生成（这里只引用其返回类型）。 */
export type ChatOrderEvidence = ReturnType<typeof import("../orderPreview").extractEvidence>;

/** 预览面板状态：pending=等用户点确认/取消；confirmed=token 已决议，等后端落库结果。 */
export interface OrderPreviewState {
  state: PreviewState;
  order_id: string;
  /** order.preview 的确认 token；真实握手必须带它 POST /commerce/orders/confirm。 */
  token: string;
  card: OrderCard;
  /** i18n key：渲染时按当前语言取文案，切换语言后提示不会停留在旧语言。 */
  hintKey: string;
  /** invocation=旧 tool.invoke 兜底、preview=order.preview 真实握手、result=已拿到订单结果。 */
  source: "invocation" | "preview" | "result";
}

/** SSE 事件留痕，用于按 trace_id 关联 invoke / result。 */
export interface SeenEvent {
  event: string;
  payload: unknown;
}

export interface TimelineEntry {
  label: string;
  detail: string;
  hits?: ProductHit[];
}

/**
 * SSE / 请求 / 子 Agent 失败状态：
 * - request：HTTP / 网络失败，带失败 query 供重试；
 * - sse：SSE 断线，只需重连，不重复提交请求；
 * - subagent：`final.result` / AIMessage 含 `[subagent unavailable]`，错误卡片挂在
 *   该轮 assistant 消息下方；重试同样重发该轮 query（同一 session id）。
 */
export interface LastError {
  kind: "request" | "sse" | "subagent";
  message?: string;
  query?: string;
  /** subagent 失败时原始标记行。 */
  detail?: string;
}
