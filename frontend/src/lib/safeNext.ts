/**
 * Куда вернуться после входа: только путь этого сайта. Не `//…` и не `/\…` (браузер читает их
 * как адрес другого сайта), без управляющих символов (табуляцию и перевод строки URL-парсер
 * выбрасывает, и `/\t/evil` становится `//evil`), не сама страница входа (в любом регистре:
 * `/LOGIN` — тоже она). Иначе — главная.
 */
export function safeNext(raw: string | undefined): string {
  if (!raw || !raw.startsWith("/")) return "/";
  if (raw.startsWith("//") || raw.includes("\\")) return "/";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(raw)) return "/";
  if (raw.toLowerCase().startsWith("/login")) return "/";
  return raw;
}
