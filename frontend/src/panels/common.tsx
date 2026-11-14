import { ApiError } from "../api/client";
import { FormNote } from "../ui/form";

/** Кнопка-ссылка действия в панели. */
export const PANEL_ACTION =
  "inline-flex items-center gap-1.5 rounded-xl border border-line px-3 py-2 text-sm font-medium transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary";

export function PanelLoading() {
  return (
    <p role="status" className="py-4 text-sm text-muted">
      Загружаю…
    </p>
  );
}

/** Ошибка загрузки: на 404 — свой текст, иначе `detail` сервера. */
export function PanelError({ error, notFound }: { error: unknown; notFound: string }) {
  const text =
    error instanceof ApiError ? (error.status === 404 ? notFound : error.detail) : "Сервис временно недоступен";
  return <FormNote tone="error">{text}</FormNote>;
}
