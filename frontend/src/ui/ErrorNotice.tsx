import { RotateCw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";

/** Сообщения браузеров (Chrome, Safari, Firefox) и Vite, когда чанк из `/assets/` не загрузился:
 * обычно сайт обновился, а открытая страница ссылается на файлы прежней сборки. */
const CHUNK_ERRORS = [
  "Failed to fetch dynamically imported module",
  "Importing a module script failed",
  "error loading dynamically imported module",
  "Unable to preload CSS",
];

export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return CHUNK_ERRORS.some((prefix) => error.message.startsWith(prefix));
}

const reloadPage = () => window.location.reload();

/** Понятное сообщение об ошибке вместо раздела. Текст ошибки не показывается: в нём бывают
 * технические подробности. */
export function ErrorNotice({ error, reload = reloadPage }: { error: unknown; reload?: () => void }) {
  const stale = isChunkLoadError(error);
  return (
    <div role="alert" className="mx-auto max-w-md rounded-[14px] bg-card p-6 text-center shadow-card">
      <p className="text-lg font-semibold">
        {stale ? "Сайт обновился — перезагрузите страницу" : "Что-то пошло не так"}
      </p>
      <p className="mt-2 text-sm text-muted">
        {stale
          ? "Открытая вкладка ссылается на файлы прежней версии."
          : "Раздел не удалось показать. Перезагрузка страницы обычно помогает."}
      </p>
      <button
        type="button"
        onClick={reload}
        className="mt-4 inline-flex items-center gap-2 rounded-xl bg-primary-deep px-4 py-2 text-sm font-medium text-on-primary transition-colors hover:bg-primary-deep/90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
      >
        <RotateCw size={15} aria-hidden="true" />
        Перезагрузить
      </button>
    </div>
  );
}

/** Ошибка маршрута (`defaultErrorComponent` роутера). */
export function RouteError({ error }: { error: unknown }) {
  return (
    <section className="px-4 py-16">
      <ErrorNotice error={error} />
    </section>
  );
}

/** Граница ошибок для части страницы (например, ленивого 3D-вида): остальное продолжает
 * работать. Классовый компонент — иначе React ошибки рендера не ловит. */
export class ErrorBoundary extends Component<{ children: ReactNode; fallback?: (error: unknown) => ReactNode }> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error ?? new Error("unknown") };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    // Для отладки в консоли разработчика; пользователю — только ErrorNotice.
    console.error(error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return this.props.fallback ? this.props.fallback(error) : <ErrorNotice error={error} />;
  }
}
