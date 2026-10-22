import { t, type Lang } from "../i18n";
import type { StoredSession } from "../sessionHistory";
import { styles } from "./styles";

/** 会话历史面板：后端列表 + 本地记录；点条目切换会话。 */
export function SessionHistoryPanel({
  lang,
  sessions,
  sessionId,
  syncing,
  syncError,
  onSwitch,
  onNew,
  onSync,
  onDelete,
}: {
  lang: Lang;
  sessions: StoredSession[];
  sessionId: string;
  syncing: boolean;
  syncError: string;
  onSwitch: (id: string) => void;
  onNew: () => void;
  onSync: () => void;
  onDelete: (id: string) => void;
}) {
  return (
    <section style={styles.history} data-testid="session-history">
      <div style={styles.previewHead}>
        <h2 style={styles.panelTitle}>{t(lang, "history.title")}</h2>
        <div style={styles.orderActions}>
          <button type="button" style={styles.chipToggle} data-testid="history-new" onClick={onNew}>
            {t(lang, "history.new")}
          </button>
          <button
            type="button"
            style={styles.chipToggle}
            data-testid="history-sync"
            disabled={syncing}
            onClick={onSync}
          >
            {syncing ? t(lang, "history.syncing") : t(lang, "history.sync")}
          </button>
        </div>
      </div>
      {sessions.length === 0 ? (
        <p style={styles.muted}>{t(lang, "history.empty")}</p>
      ) : (
        <ul style={styles.historyList}>
          {sessions.map((session) => {
            const userTurns = session.messages.filter((message) => message.role === "user").length;
            const turns = session.turns ?? userTurns;
            const fromBackend = session.source === "backend" || session.source === "both";
            return (
              <li
                key={session.id}
                style={session.id === sessionId ? { ...styles.historyItem, ...styles.historyItemActive } : styles.historyItem}
                data-testid="history-item"
              >
                <button
                  type="button"
                  style={styles.historyButton}
                  onClick={() => onSwitch(session.id)}
                  title={session.id}
                >
                  <span style={styles.historyTitle}>
                    {session.title || t(lang, "history.untitled")}
                  </span>
                  <span style={styles.historyMeta}>
                    {t(lang, "history.turns", { count: turns })}
                    {" · "}
                    {fromBackend ? t(lang, "history.fromBackend") : t(lang, "history.fromLocal")}
                    {session.id === sessionId ? ` · ${t(lang, "history.current")}` : ""}
                  </span>
                </button>
                <button
                  type="button"
                  style={styles.historyDelete}
                  data-testid="history-delete"
                  title={t(lang, "history.delete")}
                  onClick={() => onDelete(session.id)}
                >
                  ✕
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {syncError && <p style={{ ...styles.muted, color: "#b45309" }}>{syncError}</p>}
    </section>
  );
}
