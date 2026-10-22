import type { FormEvent } from "react";

import { t, suggestedQuestions, type Lang } from "../i18n";
import type { ComparisonRow, PreferenceSelection } from "../products";
import { PREVIEW_STATE } from "../orderPreview";
import { ComparisonTable } from "./ComparisonTable";
import { FeedbackPanel } from "./FeedbackPanel";
import { OrderCardView } from "./OrderCard";
import { PreferenceChips } from "./PreferenceChips";
import { ProductSkeletonStack } from "./ProductCard";
import { SubagentErrorCard } from "./SubagentErrorCard";
import { styles } from "./styles";
import type { ChatMessage, ChatOrderEvidence, OrderPreviewState } from "./types";

/**
 * 左栏：消息流 + 商品骨架屏 + 偏好 chips + 对比表 + 下单确认面板 + 输入框。
 *
 * 纯展示组件：请求、SSE、状态机都在 App.tsx；这里只按 props 渲染并把用户
 * 动作回调出去，DOM 结构（含 data-testid）与拆分前逐字一致。
 */
export function ChatPanel({
  lang,
  messages,
  query,
  sending,
  canSend,
  searchSkeletonVisible,
  comparison,
  preferences,
  chipsOpen,
  orderPreview,
  downOpenId,
  onQueryChange,
  onSubmit,
  onSuggestion,
  onRetrySubagent,
  onFeedback,
  onToggleReasons,
  onPreferencesChange,
  onToggleChips,
  onPreviewDecision,
  onLookupOrder,
  extractEvidence,
}: {
  lang: Lang;
  messages: ChatMessage[];
  query: string;
  sending: boolean;
  canSend: boolean;
  searchSkeletonVisible: boolean;
  comparison: ComparisonRow[];
  preferences: PreferenceSelection;
  chipsOpen: boolean;
  orderPreview: OrderPreviewState | null;
  downOpenId: string | null;
  onQueryChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onSuggestion: (question: string) => void;
  onRetrySubagent: (message: ChatMessage) => void;
  onFeedback: (message: ChatMessage, score: 1 | -1, reason?: string) => void;
  onToggleReasons: (id: string) => void;
  onPreferencesChange: (next: PreferenceSelection) => void;
  onToggleChips: () => void;
  onPreviewDecision: (decision: "confirm" | "cancel") => void;
  onLookupOrder: (orderId: string) => void;
  extractEvidence: (order: unknown) => ChatOrderEvidence;
}) {
  return (
    <section style={styles.panel}>
      <h2 style={styles.panelTitle}>{t(lang, "chat.title")}</h2>
      <div style={styles.chat}>
        {messages.length === 0 ? (
          <div data-testid="empty-state">
            <p style={styles.muted}>{t(lang, "chat.empty")}</p>
            <div style={styles.suggestions} data-testid="suggested-questions">
              {suggestedQuestions(lang).map((question) => (
                <button
                  key={question}
                  type="button"
                  style={styles.suggestion}
                  data-testid="suggestion"
                  onClick={() => onSuggestion(question)}
                >
                  {question}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((message) => (
            <div key={message.id} style={styles.message}>
              <strong>{message.role === "user" ? t(lang, "role.buyer") : "Agent"}：</strong>
              <span style={styles.messageText}>{message.content}</span>
              {message.role === "assistant" && message.subagentError && (
                <SubagentErrorCard
                  lang={lang}
                  detail={message.subagentDetail}
                  retrying={sending}
                  onRetry={() => onRetrySubagent(message)}
                />
              )}
              {message.role === "assistant" && message.order && message.orderEvidence && (
                <OrderCardView
                  lang={lang}
                  card={message.order}
                  evidence={message.orderEvidence}
                  onQuery={onLookupOrder}
                />
              )}
              {/* 反馈条单独成组件：App 只负责提交，展开状态留在这一层。 */}
              {message.role === "assistant" && message.content && (
                <FeedbackPanel
                  lang={lang}
                  message={message}
                  downOpen={downOpenId === message.id}
                  onToggleReasons={() => onToggleReasons(message.id)}
                  onFeedback={(score, reason) => onFeedback(message, score, reason)}
                />
              )}
            </div>
          ))
        )}
        {searchSkeletonVisible && <ProductSkeletonStack label={t(lang, "skeleton.loading")} />}
      </div>
      <PreferenceChips
        lang={lang}
        selection={preferences}
        onChange={onPreferencesChange}
        open={chipsOpen}
        onToggle={onToggleChips}
      />
      <form onSubmit={onSubmit} style={styles.composer}>
        <input
          style={styles.input}
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={t(lang, "chat.inputPlaceholder")}
        />
        <button type="submit" disabled={!canSend} style={styles.button}>
          {sending ? t(lang, "chat.sending") : t(lang, "chat.send")}
        </button>
      </form>

      {comparison.length > 0 && (
        <section style={styles.panel} data-testid="compare-panel">
          <h3 style={styles.panelTitle}>{t(lang, "compare.title")}</h3>
          <ComparisonTable lang={lang} rows={comparison} />
        </section>
      )}

      {orderPreview &&
        (orderPreview.state === PREVIEW_STATE.PENDING ||
          orderPreview.state === PREVIEW_STATE.CONFIRMED) && (
          <section style={styles.preview} data-testid="order-preview">
            <div style={styles.previewHead}>
              <strong>{t(lang, "order.previewTitle")}</strong>
            </div>
            <OrderCardView
              lang={lang}
              card={orderPreview.card}
              evidence={extractEvidence({
                order_id: orderPreview.card.order_id,
                status: orderPreview.card.status,
              })}
              badgeOverride={
                orderPreview.state === PREVIEW_STATE.CONFIRMED
                  ? { tone: "green", text: t(lang, "order.confirmingBadge") }
                  : { tone: "amber", text: t(lang, "order.pendingBadge") }
              }
              onQuery={onLookupOrder}
            />
            <div style={styles.badgeHint}>{t(lang, orderPreview.hintKey)}</div>
            {orderPreview.state === PREVIEW_STATE.PENDING && (
              <div style={styles.orderActions}>
                <button
                  type="button"
                  style={styles.primaryButton}
                  data-testid="order-confirm"
                  onClick={() => onPreviewDecision("confirm")}
                >
                  {t(lang, "order.confirm")}
                </button>
                <button
                  type="button"
                  style={styles.ghostButton}
                  data-testid="order-cancel"
                  onClick={() => onPreviewDecision("cancel")}
                >
                  {t(lang, "order.cancel")}
                </button>
              </div>
            )}
          </section>
        )}
    </section>
  );
}

