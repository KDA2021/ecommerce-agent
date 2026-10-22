import { t, type Lang } from "../i18n";
import { priceText, shipsToText, type ProductHit } from "../products";
import { styles } from "./styles";

/** 推荐理由 code → 当前语言；未知 code 用后端给的 label（不编造）。 */
function localizedReasonTexts(lang: Lang, reasons: ProductHit["reasons"]): string[] {
  return reasons.map((reason) => {
    const key = `product.reason.${reason.code}`;
    const localized = t(lang, key);
    const base = localized === key ? reason.label : localized;
    return reason.code === "preference_match" && reason.matched_preference
      ? `${base}：${reason.matched_preference}`
      : base;
  });
}

/** 商品卡：标题 + 价格 + 推荐理由（只渲染后端给的）+ 亮点。 */
export function ProductCardView({ lang, hit }: { lang: Lang; hit: ProductHit }) {
  const reasons = localizedReasonTexts(lang, hit.reasons);
  return (
    <div style={styles.card} data-testid="product-card">
      <div style={styles.cardTitle}>{hit.title}</div>
      <div style={styles.muted}>
        {hit.product_id}
        {hit.category ? ` · ${hit.category}` : ""}
        {hit.origin_country ? ` · ${t(lang, "product.origin")} ${hit.origin_country}` : ""}
      </div>
      <div style={styles.cardPrice}>{priceText(hit)}</div>
      {reasons.length > 0 ? (
        <div style={styles.reasonRow} data-testid="product-reasons">
          {reasons.map((text) => (
            <span key={text} style={styles.reasonChip}>
              {text}
            </span>
          ))}
        </div>
      ) : (
        <div style={styles.reasonEmpty} data-testid="product-reasons-empty">
          {t(lang, "product.reasonsEmpty")}
        </div>
      )}
      <div style={styles.cardHighlights}>
        {t(lang, "product.shipsTo")}：{shipsToText(hit)}
        {hit.highlights.length > 0 ? ` ｜ ${hit.highlights.join("；")}` : ""}
      </div>
    </div>
  );
}

/** 流式 / 检索期间的商品卡骨架屏（不展示任何占位数据，只表示"正在加载"）。 */
export function ProductSkeleton({ label }: { label: string }) {
  return (
    <div style={styles.skeletonCard} data-testid="product-skeleton" aria-busy="true" aria-label={label}>
      <div style={{ ...styles.skeletonLine, width: "72%", marginTop: 0 }} />
      <div style={{ ...styles.skeletonLine, width: "44%" }} />
      <div style={{ ...styles.skeletonLine, width: "60%" }} />
      <div style={{ ...styles.skeletonLine, width: "36%" }} />
    </div>
  );
}

export function ProductSkeletonStack({ label }: { label: string }) {
  return (
    <div style={styles.skeletonStack} data-testid="product-skeleton-stack">
      <ProductSkeleton label={label} />
      <ProductSkeleton label={label} />
    </div>
  );
}
