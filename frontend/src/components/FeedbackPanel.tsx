import { FEEDBACK_REASONS } from "../api";
import { feedbackReasonLabel, t, type Lang } from "../i18n";
import { styles } from "./styles";
import type { ChatMessage } from "./types";

/**
 * 单条 assistant 回复的反馈条（点赞 / 点踩 + 点踩理由 chips）。
 *
 * 原本内联在 App.tsx 的消息循环里；拆出后 App 只负责提交反馈，展开状态
 * 由父组件持有（`downOpenId`），保证「一次只开一个理由列表」的行为不变。
 */
export function FeedbackPanel({
  lang,
  message,
  downOpen,
  onToggleReasons,
  onFeedback,
}: {
  lang: Lang;
  message: ChatMessage;
  downOpen: boolean;
  onToggleReasons: () => void;
  onFeedback: (score: 1 | -1, reason?: string) => void;
}) {
  return (
    <div style={styles.feedback}>
      {message.feedbackStatus === "done" ? (
        <span style={styles.thanks}>
          {message.feedback === 1
            ? t(lang, "feedback.thanksLike")
            : t(lang, "feedback.thanksDislike", {
                reason: feedbackReasonLabel(lang, message.feedbackReason ?? ""),
              })}
        </span>
      ) : (
        <>
          <button
            type="button"
            style={styles.thumb}
            title={t(lang, "feedback.like")}
            disabled={message.feedbackStatus === "sending"}
            onClick={() => onFeedback(1)}
          >
            👍
          </button>
          <button
            type="button"
            style={styles.thumb}
            title={t(lang, "feedback.dislike")}
            disabled={message.feedbackStatus === "sending"}
            onClick={onToggleReasons}
          >
            👎
          </button>
          {message.feedbackStatus === "error" && (
            <span style={{ color: "#b91c1c", fontSize: 12 }}>{t(lang, "feedback.failed")}</span>
          )}
          {downOpen &&
            FEEDBACK_REASONS.map((reason) => (
              <button
                key={reason.code}
                type="button"
                style={styles.chip}
                onClick={() => onFeedback(-1, reason.code)}
              >
                {feedbackReasonLabel(lang, reason.code)}
              </button>
            ))}
        </>
      )}
    </div>
  );
}
