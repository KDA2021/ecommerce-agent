import { t, type Lang } from "../i18n";
import { styles } from "./styles";

/**
 * P12-3 子 Agent 失败卡片。
 *
 * 只补充“哪个子任务失败 + 重试”，不隐藏本轮已拿到的 partial 结果：卡片渲染在
 * assistant 正文下方，正文 / 商品卡片 / 对比表照常显示。
 */
export function SubagentErrorCard({
  lang,
  detail,
  retrying,
  onRetry,
}: {
  lang: Lang;
  detail?: string;
  retrying: boolean;
  onRetry: () => void;
}) {
  return (
    <div style={styles.errorCard} data-testid="error-card" role="alert">
      <strong style={styles.errorCardTitle}>{t(lang, "error.subagent")}</strong>
      {detail && <code style={styles.errorCardDetail}>{detail}</code>}
      <p style={styles.errorCardHint}>{t(lang, "error.subagentHint")}</p>
      <button
        type="button"
        style={styles.retryButton}
        data-testid="subagent-retry"
        disabled={retrying}
        onClick={onRetry}
      >
        {t(lang, "error.retrySubagent")}
      </button>
    </div>
  );
}
