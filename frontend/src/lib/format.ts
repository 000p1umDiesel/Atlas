/** Целое число в русской записи: 1 260. */
export function formatInt(n: number): string {
  return n.toLocaleString("ru-RU");
}

/** Дробная метрика (PageRank, вес): три значащие цифры, запятая. */
export function formatScore(n: number): string {
  return n.toLocaleString("ru-RU", { maximumSignificantDigits: 3 });
}

/** Русское множественное число: plural(5, ["документ", "документа", "документов"]). */
export function plural(n: number, forms: [string, string, string]): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

/** «1 260 фрагментов». */
export function countOf(n: number, forms: [string, string, string]): string {
  return `${formatInt(n)} ${plural(n, forms)}`;
}

const DAY = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" });
const DAY_TIME = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });

export function formatDay(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : DAY.format(d);
}

export function formatDayTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : DAY_TIME.format(d);
}

const RELATIVE = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });

/** «5 минут назад» по unix-секундам; `null` — «никогда». */
export function ago(unixSeconds: number | null): string {
  if (unixSeconds === null) return "никогда";
  const diff = unixSeconds - Date.now() / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return RELATIVE.format(Math.round(diff), "second");
  if (abs < 3600) return RELATIVE.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return RELATIVE.format(Math.round(diff / 3600), "hour");
  return RELATIVE.format(Math.round(diff / 86400), "day");
}

/** Длительность в секундах: «1,2 с»; меньше 100 мс — «<0,1 с». */
export function seconds(ms: number): string {
  if (ms < 100) return "<0,1 с";
  return `${(ms / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} с`;
}

/** Длиннее этого подпись узла на графе обрезается: полное имя — в подсказке и карточке. */
export const LABEL_MAX_CHARS = 32;

/** Короткая подпись: длинное имя обрезается с «…» (по символам, не по UTF-16). */
export function truncateLabel(name: string, max = LABEL_MAX_CHARS): string {
  const chars = Array.from(name.trim());
  return chars.length > max ? `${chars.slice(0, max - 1).join("").trimEnd()}…` : chars.join("");
}
