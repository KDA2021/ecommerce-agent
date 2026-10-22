import { useCallback, useEffect, useState, type CSSProperties } from "react";

import { FEEDBACK_REASONS, getDashboard, type DashboardMetrics } from "./api";
import { feedbackReasonLabel, nextLang, t, type Lang } from "./i18n";

const percent = (value: number): string => `${(value * 100).toFixed(1)}%`;

/** P10-5：把布尔开关渲染成当前语言的「开启 / 关闭」。 */
const onOff = (lang: Lang, enabled: boolean): string =>
  t(lang, enabled ? "dashboard.on" : "dashboard.off");

const styles: Record<string, CSSProperties> = {
  page: {
    maxWidth: 1120,
    margin: "0 auto",
    padding: 24,
    color: "#1f2937",
    fontFamily: "system-ui, sans-serif",
  },
  header: { display: "flex", alignItems: "baseline", gap: 16, flexWrap: "wrap" },
  title: { margin: "0 0 4px", fontSize: 24 },
  muted: { margin: "4px 0", color: "#6b7280", fontSize: 13 },
  link: {
    border: "none",
    background: "transparent",
    color: "#2563eb",
    cursor: "pointer",
    fontSize: 14,
    padding: 0,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
    gap: 12,
    marginTop: 20,
  },
  card: {
    padding: 14,
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    background: "#fff",
  },
  cardLabel: { margin: 0, color: "#6b7280", fontSize: 13 },
  cardValue: { margin: "6px 0 0", fontSize: 26, fontWeight: 700 },
  cardHint: { margin: "4px 0 0", color: "#9ca3af", fontSize: 12 },
  panel: {
    marginTop: 20,
    padding: 14,
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    background: "#fff",
  },
  panelTitle: { margin: "0 0 10px", fontSize: 16 },
  row: { display: "flex", alignItems: "center", gap: 10, margin: "6px 0", fontSize: 14 },
  barTrack: { flex: 1, height: 10, background: "#f3f4f6", borderRadius: 999 },
  barFill: { height: 10, background: "#f97316", borderRadius: 999 },
  count: { width: 40, textAlign: "right", color: "#4b5563" },
};

export default function Dashboard({
  lang,
  onBack,
  onLangChange,
}: {
  lang: Lang;
  onBack: () => void;
  onLangChange: (lang: Lang) => void;
}) {
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setMetrics(await getDashboard());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 后端 reason code 白名单与前端一致，这里只用 code 本地化（未知 code 原样显示）。
  const reasons = metrics ? Object.entries(metrics.reason_distribution) : [];
  const reasonTotal = reasons.reduce((sum, [, value]) => sum + value, 0);
  const maxReason = Math.max(1, ...reasons.map(([, value]) => value));
  const knownCodes = new Set<string>(FEEDBACK_REASONS.map((item) => item.code));

  return (
    <main style={styles.page}>
      <header style={styles.header}>
        <h1 style={styles.title}>{t(lang, "dashboard.title")}</h1>
        <button type="button" style={styles.link} onClick={onBack}>
          {t(lang, "dashboard.back")}
        </button>
        <button type="button" style={styles.link} onClick={() => void load()}>
          {loading ? t(lang, "dashboard.refreshing") : t(lang, "dashboard.refresh")}
        </button>
        <button
          type="button"
          style={styles.link}
          data-testid="lang-toggle"
          onClick={() => onLangChange(nextLang(lang))}
        >
          {t(lang, "lang.toggle")}
        </button>
      </header>
      <p style={styles.muted}>{t(lang, "dashboard.source")}</p>

      {error && <p style={{ ...styles.muted, color: "#b91c1c" }}>{error}</p>}

      {metrics && (
        <>
          <section style={styles.grid}>
            <MetricCard
              label={t(lang, "dashboard.sessions")}
              value={String(metrics.sessions)}
              hint={t(lang, "dashboard.sessionsHint", { count: metrics.trace_sessions })}
            />
            <MetricCard
              label={t(lang, "dashboard.avgTurns")}
              value={metrics.avg_turns.toFixed(2)}
              hint={t(lang, "dashboard.avgTurnsHint", { count: metrics.turns })}
            />
            <MetricCard
              label={t(lang, "dashboard.completion")}
              value={percent(metrics.completion_rate)}
              hint={t(lang, "dashboard.completionHint")}
            />
            <MetricCard
              label={t(lang, "dashboard.votes")}
              value={`${metrics.likes} / ${metrics.dislikes}`}
            />
            <MetricCard
              label="TTFT"
              value={`${metrics.ttft_ms.toFixed(0)} ms`}
              hint={t(lang, "dashboard.ttftHint", { count: metrics.ttft_samples })}
            />
            <MetricCard
              label={t(lang, "dashboard.cache")}
              value={`${metrics.cache_hits}`}
              hint={t(lang, "dashboard.cacheHint", { rate: percent(metrics.cache_hit_rate) })}
            />
            <MetricCard
              label={t(lang, "dashboard.orderSuccess")}
              value={String(metrics.order_success)}
              hint={t(lang, "dashboard.orderSuccessHint")}
            />
            <MetricCard
              label={t(lang, "dashboard.cost")}
              value={`${metrics.cost_currency} ${metrics.cost_estimate.toFixed(4)}`}
              hint={t(lang, "dashboard.costHint", {
                tokens: metrics.total_tokens_estimate,
                price: metrics.price_per_1k_tokens,
              })}
            />
            <MetricCard
              label={t(lang, "dashboard.llmCalls")}
              value={String(metrics.llm_calls)}
              hint={t(lang, "dashboard.errorsHint", { count: metrics.errors })}
            />
          </section>

          {/* P10-5：当前 A/B 实验开关（启动时从环境变量读取，Dashboard 只读展示）。 */}
          {metrics.experiment ? (
            <section style={styles.panel} data-testid="experiment-config">
              <h2 style={styles.panelTitle}>{t(lang, "dashboard.experiment")}</h2>
              <ExperimentRow
                label={t(lang, "dashboard.semanticCache")}
                value={onOff(lang, metrics.experiment.semantic_cache_enabled)}
              />
              <ExperimentRow
                label={t(lang, "dashboard.reranker")}
                value={onOff(lang, metrics.experiment.reranker_enabled)}
              />
              <ExperimentRow
                label={t(lang, "dashboard.modelRoute")}
                value={metrics.experiment.model_route_mode}
              />
              <p style={styles.muted}>{t(lang, "dashboard.experimentHint")}</p>
            </section>
          ) : (
            <p style={{ ...styles.muted, marginTop: 16 }}>
              {t(lang, "dashboard.experimentLoadFailed")}
            </p>
          )}

          <section style={styles.panel}>
            <h2 style={styles.panelTitle}>
              {t(lang, "dashboard.reasons", { count: reasonTotal })}
            </h2>
            {reasonTotal === 0 ? (
              <p style={styles.muted}>{t(lang, "dashboard.noReasons")}</p>
            ) : (
              reasons.map(([code, value]) => (
                <div key={code} style={styles.row}>
                  <span style={{ width: 96 }}>
                    {knownCodes.has(code) ? feedbackReasonLabel(lang, code) : code}
                  </span>
                  <span style={styles.barTrack}>
                    <span style={{ ...styles.barFill, width: `${(value / maxReason) * 100}%` }} />
                  </span>
                  <span style={styles.count}>{value}</span>
                </div>
              ))
            )}
          </section>
        </>
      )}
    </main>
  );
}

function MetricCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div style={styles.card}>
      <p style={styles.cardLabel}>{label}</p>
      <p style={styles.cardValue}>{value}</p>
      {hint && <p style={styles.cardHint}>{hint}</p>}
    </div>
  );
}

/** P10-5：实验配置的一行「名称 —— 取值」。 */
function ExperimentRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={styles.row}>
      <span style={{ width: 160, color: "#6b7280" }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
    </div>
  );
}
