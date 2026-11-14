// Общие куски вкладок страницы сущности.
import { ApiError } from "../api/client";
import { GHOST_BUTTON } from "../ui/styles";
import { formatInt } from "../lib/format";

/** Текст ошибки запроса: `detail` сервера. */
export const errorText = (error: unknown) => (error instanceof ApiError ? error.detail : "Ошибка запроса");

/** «Показать ещё» и счётчик «N из M» под списком вкладки. */
export function LoadMore({ shown, total, disabled, onClick }: { shown: number; total: number; disabled: boolean; onClick(): void }) {
  return (
    <div className="mt-4 flex items-center gap-3">
      <button type="button" disabled={disabled} onClick={onClick} className={GHOST_BUTTON}>
        Показать ещё
      </button>
      <span className="text-sm text-muted tabular-nums">
        {formatInt(shown)} из {formatInt(total)}
      </span>
    </div>
  );
}

