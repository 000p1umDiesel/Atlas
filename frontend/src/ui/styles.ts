// Общие классы Tailwind: таблицы, ссылка-имя, «призрачная» кнопка и хлебные крошки.
export const TABLE = "w-full border-collapse text-sm";
export const TH = "border-b border-line py-1.5 pr-3 text-left font-medium text-muted";
export const TD = "border-b border-line py-2.5 pr-3 align-top";
export const NAME_LINK =
  "font-medium text-ink underline-offset-4 [overflow-wrap:anywhere] hover:text-primary-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary";
export const GHOST_BUTTON =
  "inline-flex items-center rounded-xl border border-line px-3 py-1.5 text-sm font-medium transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-wait disabled:opacity-60";

/** Хлебная крошка «назад» над заголовком страницы. */
export const CRUMB =
  "text-sm text-muted underline-offset-4 hover:text-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary";
