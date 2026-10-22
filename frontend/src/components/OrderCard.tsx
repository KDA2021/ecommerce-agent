import { t, type Lang } from "../i18n";
import { extractEvidence, type OrderCard } from "../orderPreview";
import { styles } from "./styles";

/** 金额展示：未知时用占位符，避免前端编造价格。 */
function formatAmount(lang: Lang, value: number | undefined, currency: string): string {
  if (typeof value !== "number") return t(lang, "order.amountPending");
  return currency ? `${value} ${currency}` : String(value);
}

const ORDER_STATUS_KEYS: Record<string, string> = {
  待确认: "order.status.pending",
  已确认: "order.status.confirmed",
  已取消: "order.status.cancelled",
  未知: "order.status.unknown",
};

/** 后端 / orderPreview 的状态中文 label → 当前语言。 */
function localizedStatusLabel(lang: Lang, label: string): string {
  const key = ORDER_STATUS_KEYS[label];
  return key ? t(lang, key) : label;
}

/** 订单卡片（行项目 + 金额 + 状态 + 验证徽章），预览与结果共用。 */
export function OrderCardView({
  lang,
  card,
  evidence,
  onQuery,
  badgeOverride,
}: {
  lang: Lang;
  card: OrderCard;
  evidence: ReturnType<typeof extractEvidence>;
  onQuery?: (orderId: string) => void;
  badgeOverride?: { tone: "green" | "amber"; text: string };
}) {
  const badgeTone = badgeOverride ? badgeOverride.tone : evidence.badge.tone;
  const badgeLabel = badgeOverride
    ? badgeOverride.text
    : t(lang, badgeTone === "green" ? "order.verifiedBadge" : "order.unverifiedBadge");
  const badgeText = badgeOverride ? badgeLabel : `${badgeLabel} · ${evidence.badge.short}`;
  return (
    <div style={styles.preview} data-testid="order-card">
      <div style={styles.previewHead}>
        <strong>
          {t(lang, "order.cardPrefix")} {card.order_id || t(lang, "order.pendingId")}
        </strong>
        <span style={badgeTone === "green" ? styles.badgeGreen : styles.badgeAmber}>
          {badgeTone === "green" ? "✓ " : "⚠ "}
          {badgeText}
        </span>
      </div>
      <div style={styles.orderMeta}>
        {t(lang, "order.status")}：{localizedStatusLabel(lang, card.status_label)} ·{" "}
        {t(lang, "order.itemCount", { count: card.item_count })}
      </div>
      {card.items.map((item) => (
        <div key={item.key} style={styles.orderLine}>
          <span>
            {item.title}
            {item.sku_id ? `（${item.sku_id}）` : ""} × {item.quantity}
          </span>
          <span>{formatAmount(lang, item.unit_price_major, card.currency)}</span>
        </div>
      ))}
      <div style={styles.orderLine}>
        <span>{t(lang, "order.subtotal")}</span>
        <span>{formatAmount(lang, card.subtotal_major, card.currency)}</span>
      </div>
      <div style={styles.orderTotal}>
        {t(lang, "order.total")}：{formatAmount(lang, card.total_major, card.currency)}
      </div>
      {card.address_text && (
        <div style={styles.orderMeta}>
          {t(lang, "order.address")}：{card.address_text}
        </div>
      )}
      {card.cancel_reason && (
        <div style={styles.orderMeta}>
          {t(lang, "order.cancelReason")}：{card.cancel_reason}
        </div>
      )}
      {!badgeOverride && (
        <div style={styles.badgeHint}>
          {t(lang, badgeTone === "green" ? "order.verifiedHint" : "order.unverifiedHint")}
        </div>
      )}
      {evidence.badge.queryable && card.order_id && onQuery && (
        <div style={styles.orderActions}>
          <button type="button" style={styles.ghostButton} onClick={() => onQuery(card.order_id)}>
            {t(lang, "order.query")}
          </button>
        </div>
      )}
    </div>
  );
}
