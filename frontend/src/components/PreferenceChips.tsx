import { t, type Lang } from "../i18n";
import {
  BUDGET_CHIPS,
  CATEGORY_CHIPS,
  COUNTRY_CHIPS,
  type PreferenceSelection,
} from "../products";
import { styles } from "./styles";

/** 偏好 chips：预算区间 / 收货国家 / 品类，选择后随请求作为 preference_hint 提交。 */
export function PreferenceChips({
  lang,
  selection,
  onChange,
  open,
  onToggle,
}: {
  lang: Lang;
  selection: PreferenceSelection;
  onChange: (next: PreferenceSelection) => void;
  open: boolean;
  onToggle: () => void;
}) {
  const countryOptions = COUNTRY_CHIPS.includes(selection.shipTo as (typeof COUNTRY_CHIPS)[number])
    ? COUNTRY_CHIPS
    : selection.shipTo
      ? ([...COUNTRY_CHIPS, selection.shipTo] as readonly string[])
      : COUNTRY_CHIPS;
  const categoryOptions = CATEGORY_CHIPS.includes(selection.category as (typeof CATEGORY_CHIPS)[number])
    ? CATEGORY_CHIPS
    : selection.category
      ? ([...CATEGORY_CHIPS, selection.category] as readonly string[])
      : CATEGORY_CHIPS;

  function toggleBudget(value: number) {
    onChange({ ...selection, budgetMaxMajor: selection.budgetMaxMajor === value ? null : value });
  }

  function toggleCountry(value: string) {
    onChange({ ...selection, shipTo: selection.shipTo === value ? "" : value });
  }

  function toggleCategory(value: string) {
    onChange({ ...selection, category: selection.category === value ? "" : value });
  }

  return (
    <section style={styles.chipBar} data-testid="preference-chips">
      <div style={styles.chipGroup}>
        <span style={styles.chipGroupLabel}>{t(lang, "preference.budget")}</span>
        {open ? (
          <>
            {BUDGET_CHIPS.map((budget) => (
              <button
                key={`budget-${budget}`}
                type="button"
                data-testid={`chip-budget-${budget}`}
                style={selection.budgetMaxMajor === budget ? styles.chipOptionActive : styles.chipOption}
                onClick={() => toggleBudget(budget)}
              >
                {t(lang, "preference.within", { amount: budget, currency: selection.budgetCurrency })}
              </button>
            ))}
            <button
              type="button"
              data-testid="chip-budget-clear"
              style={styles.chipToggle}
              onClick={() => onChange({ ...selection, budgetMaxMajor: null })}
            >
              {t(lang, "preference.any")}
            </button>
          </>
        ) : (
          <span style={styles.muted}>
            {selection.budgetMaxMajor === null
              ? t(lang, "preference.any")
              : t(lang, "preference.within", {
                  amount: selection.budgetMaxMajor,
                  currency: selection.budgetCurrency,
                })}
          </span>
        )}
      </div>

      <div style={styles.chipGroup}>
        <span style={styles.chipGroupLabel}>{t(lang, "preference.country")}</span>
        {open ? (
          <>
            {countryOptions.map((country) => (
              <button
                key={`country-${country}`}
                type="button"
                data-testid={`chip-country-${country}`}
                style={selection.shipTo === country ? styles.chipOptionActive : styles.chipOption}
                onClick={() => toggleCountry(country)}
              >
                {country}
              </button>
            ))}
            <button
              type="button"
              data-testid="chip-country-clear"
              style={styles.chipToggle}
              onClick={() => onChange({ ...selection, shipTo: "" })}
            >
              {t(lang, "preference.any")}
            </button>
          </>
        ) : (
          <span style={styles.muted}>{selection.shipTo || t(lang, "preference.any")}</span>
        )}
      </div>

      <div style={styles.chipGroup}>
        <span style={styles.chipGroupLabel}>{t(lang, "preference.category")}</span>
        {open ? (
          <>
            {categoryOptions.map((category) => (
              <button
                key={`category-${category}`}
                type="button"
                data-testid={`chip-category-${category}`}
                style={selection.category === category ? styles.chipOptionActive : styles.chipOption}
                onClick={() => toggleCategory(category)}
              >
                {category}
              </button>
            ))}
            <button
              type="button"
              data-testid="chip-category-clear"
              style={styles.chipToggle}
              onClick={() => onChange({ ...selection, category: "" })}
            >
              {t(lang, "preference.any")}
            </button>
          </>
        ) : (
          <span style={styles.muted}>{selection.category || t(lang, "preference.any")}</span>
        )}
      </div>

      <button type="button" style={styles.chipToggle} data-testid="preference-toggle" onClick={onToggle}>
        {open ? t(lang, "preference.collapse") : t(lang, "preference.expand")}
      </button>
    </section>
  );
}
