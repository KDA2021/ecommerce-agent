import { t, type Lang } from "../i18n";
import type { ComparisonRow } from "../products";
import { styles } from "./styles";

/** 前 3 个商品的对比表：价格 / 重量 / 可发国家 / 亮点（+ 推荐理由）。 */
export function ComparisonTable({ lang, rows }: { lang: Lang; rows: ComparisonRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div style={styles.compareWrap} data-testid="product-compare">
      <table style={styles.compareTable}>
        <thead>
          <tr>
            <th style={styles.compareHead}>{t(lang, "compare.product")}</th>
            <th style={styles.compareHead}>{t(lang, "compare.price")}</th>
            <th style={styles.compareHead}>{t(lang, "compare.landed")}</th>
            <th style={styles.compareHead}>{t(lang, "compare.weight")}</th>
            <th style={styles.compareHead}>{t(lang, "compare.countries")}</th>
            <th style={styles.compareHead}>{t(lang, "compare.highlights")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.product_id}-${row.title}`}>
              <td style={styles.compareCell}>
                <strong>{row.title}</strong>
                <div style={styles.muted}>
                  {row.product_id}
                  {row.reasons.length > 0 ? ` · ${row.reasons.join("、")}` : ""}
                </div>
              </td>
              <td style={styles.compareCell}>{row.price}</td>
              <td style={styles.compareCell}>{row.landedTotal}</td>
              <td style={styles.compareCell}>{row.weight}</td>
              <td style={styles.compareCell}>{row.shipsTo}</td>
              <td style={styles.compareCell}>{row.highlights.join("；") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
