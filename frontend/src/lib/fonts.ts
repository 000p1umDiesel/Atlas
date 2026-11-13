/** Шрифт подписей на холстах графа (тот же, что у интерфейса). */
export const GRAPH_FONT_FAMILY = "Golos Text Variable";

/** Латиница и кириллица: у шрифта два файла по unicode-range, нужны оба. */
const SAMPLE = "Aa Zz Аа Яя 0";

/** CSS-строка шрифта для canvas/`document.fonts`: `600 13px "Golos Text Variable"`. */
export function graphFont(weight: number, sizePx: number): string {
  return `${weight} ${sizePx}px "${GRAPH_FONT_FAMILY}"`;
}

function fontSet(): FontFaceSet | null {
  return typeof document !== "undefined" && "fonts" in document ? document.fonts : null;
}

/** Все шрифты уже загружены (или браузер не умеет FontFaceSet — тогда ждать нечего). */
export function fontsReady(fonts: string[]): boolean {
  const set = fontSet();
  if (!set) return true;
  try {
    return fonts.every((f) => set.check(f, SAMPLE));
  } catch {
    return true;
  }
}

/**
 * Загрузить шрифты до первой отрисовки WebGL-подписей: атлас глифов Sigma рисуется один раз,
 * и без этого подписи навсегда остались бы в запасном шрифте. Не дольше `timeoutMs`.
 */
export function loadFonts(fonts: string[], timeoutMs = 2000): Promise<void> {
  const set = fontSet();
  if (!set) return Promise.resolve();
  const load = Promise.all(fonts.map((f) => set.load(f, SAMPLE))).then(
    () => undefined,
    () => undefined,
  );
  const timeout = new Promise<void>((resolve) => setTimeout(resolve, timeoutMs));
  return Promise.race([load, timeout]);
}
