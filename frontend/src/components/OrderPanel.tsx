import type { FormEvent } from "react";

import { t, type Lang } from "../i18n";
import { PREVIEW_STATE } from "../orderPreview";
import { OrderCardView } from "./OrderCard";
import { ProductCardView } from "./ProductCard";
import { styles } from "./styles";
import type { ChatOrderEvidence, OrderPreviewState, TimelineEntry } from "./types";

/**
 * 右栏：SSE / 工具事件时间线 + 商品卡 + 订单证据查询。
 *
 * 订单结果卡片与左栏共用 OrderCardView；时间线里的商品命中用 ProductCardView
 * 渲染，两者都不在这里发请求，只回调 \`onLookupOrder\`。
 */
export function OrderPanel({
  lang,
  sending,
  timeline,
  orderLookupId,
  orderLookupError,
  orderPreview,
  onOrderLookupIdChange,
  onSubmitLookup,
  onLookupOrder,
  extractEvidence,
}: {
  lang: Lang;
  sending: boolean;
  timeline: TimelineEntry[];
  orderLookupId: string;
  orderLookupError: string;
  orderPreview: OrderPreviewState | null;
  onOrderLookupIdChange: (value: string) => void;
  onSubmitLookup: (event: FormEvent<HTMLFormElement>) => void;
  onLookupOrder: (orderId: string) => void;
  extractEvidence: (order: unknown) => ChatOrderEvidence;
}) {
  return (
    <section style={styles.panel}>
      <h2 style={styles.panelTitle}>{t(lang, "timeline.title")}</h2>
      <div style={styles.timeline}>
        {timeline.length === 0 ? (
          <p style={styles.muted}>
            {sending ? t(lang, "timeline.searching") : t(lang, "timeline.waiting")}
          </p>
        ) : (
          timeline.map((entry, index) => (
            <div key={`${entry.label}-${index}`} style={styles.timelineItem}>
              <div>
                <strong>{entry.label}</strong>
                <span style={styles.timelineDetail}>{entry.detail}</span>
              </div>
              {entry.hits && entry.hits.length > 0 && (
                <div style={styles.cards}>
                  {entry.hits.map((hit) => (
                    <ProductCardView key={`${hit.product_id}-${hit.title}`} lang={lang} hit={hit} />
                  ))}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      <h2 style={styles.panelTitle}>{t(lang, "order.lookupTitle")}</h2>
      <p style={styles.muted}>{t(lang, "order.lookupHint")}</p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "6px 0" }}>
        <span style={styles.badgeGreen}>✓ {t(lang, "order.verifiedBadge")} · VERIFIED</span>
        <span style={styles.badgeAmber}>⚠ {t(lang, "order.unverifiedBadge")} · UNKNOWN</span>
      </div>
      <form style={styles.queryRow} onSubmit={onSubmitLookup}>
        <input
          style={styles.input}
          value={orderLookupId}
          onChange={(event) => onOrderLookupIdChange(event.target.value)}
          placeholder={t(lang, "order.lookupPlaceholder")}
          data-testid="order-lookup-input"
        />
        <button type="submit" style={styles.button} disabled={!orderLookupId.trim()}>
          {t(lang, "order.query")}
        </button>
      </form>
      {orderLookupError && (
        <div style={{ color: "#b91c1c", fontSize: 12, marginTop: 6 }}>{orderLookupError}</div>
      )}
      {orderPreview && orderPreview.state !== PREVIEW_STATE.PENDING && (
        <OrderCardView
          lang={lang}
          card={orderPreview.card}
          evidence={extractEvidence({
            order_id: orderPreview.card.order_id,
            status: orderPreview.card.status,
            verification_status: orderPreview.source === "result" ? "VERIFIED" : "UNKNOWN",
          })}
          onQuery={onLookupOrder}
        />
      )}
    </section>
  );
}
