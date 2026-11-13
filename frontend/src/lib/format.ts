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
