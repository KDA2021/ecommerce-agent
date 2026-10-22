import Dashboard from "../Dashboard";
import type { Lang } from "../i18n";

/**
 * `/dashboard` 路由视图。
 *
 * 组件本身留在 `src/Dashboard.tsx`（自带指标拉取 / i18n）；这里只做一层
 * 路由包装，让 App.tsx 保持「组合层」：路由分支只决定渲染哪个视图，
 * 具体视图内部的取数逻辑不下沉到 App。
 */
export function DashboardView({
  lang,
  onLangChange,
  onBack,
}: {
  lang: Lang;
  onLangChange: (lang: Lang) => void;
  onBack: () => void;
}) {
  return <Dashboard lang={lang} onLangChange={onLangChange} onBack={onBack} />;
}
