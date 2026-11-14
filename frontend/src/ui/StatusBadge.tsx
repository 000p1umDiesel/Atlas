import { labelOf, STATUS_LABELS } from "../lib/labels";

/** Цвет по смыслу: ошибка, готово, в работе; остальное (ждёт, в очереди, заменена) — приглушённо. */
const TONE: Readonly<Record<string, string>> = {
  failed: "bg-danger/10 text-danger",
  done: "bg-ok/12 text-ok",
  running: "bg-primary/15 text-primary-ink",
  processing: "bg-primary/15 text-primary-ink",
};

/** Статус документа или задачи русской подписью; неизвестное значение — как есть. */
export function StatusBadge({ value }: { value: string }) {
  const tone = Object.hasOwn(TONE, value) ? TONE[value] : "bg-ink/[0.06] text-muted";
  return (
    <span data-status={value} className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${tone}`}>
      {labelOf(STATUS_LABELS, value)}
    </span>
  );
}
