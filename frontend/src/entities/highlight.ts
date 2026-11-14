// Подсветка имени сущности в тексте фрагмента: разбиение строки, без HTML.

/** Кусок текста: совпал ли он с одной из игл. */
export type TextPart = { text: string; match: boolean };

const MIN_NEEDLE = 2;
const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * Делит `text` на куски по вхождениям игл (имя и алиасы) без учёта регистра. Длинные иглы
 * проверяются первыми («Ada Lovelace» раньше «Ada»), иглы короче двух символов пропускаются,
 * спецсимволы ищутся буквально.
 */
export function splitMatches(text: string, needles: string[]): TextPart[] {
  if (!text) return [];
  const uniq = [...new Set(needles.map((n) => n.trim()).filter((n) => n.length >= MIN_NEEDLE))];
  if (uniq.length === 0) return [{ text, match: false }];
  uniq.sort((a, b) => b.length - a.length);
  const pattern = new RegExp(uniq.map(escapeRegExp).join("|"), "gi");
  const parts: TextPart[] = [];
  let at = 0;
  for (const m of text.matchAll(pattern)) {
    const start = m.index;
    if (start > at) parts.push({ text: text.slice(at, start), match: false });
    parts.push({ text: m[0], match: true });
    at = start + m[0].length;
  }
  if (at < text.length) parts.push({ text: text.slice(at), match: false });
  return parts;
}
