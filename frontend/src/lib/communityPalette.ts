import { oklchToHex } from "./color";

export interface ThemedColor {
  light: string;
  dark: string;
}

/** Золотой угол: каждый следующий оттенок максимально далёк от всех предыдущих. */
const GOLDEN_ANGLE = 137.508;
/** Первый оттенок — тёплая глина, как первый тип. */
const HUE_START = 30;
/**
 * Три яруса светлоты по кругу: соседи по списку отличаются не только оттенком,
 * но и светлотой — так различимость держится и при нарушениях цветовосприятия.
 * Светлая тема — в полосе dataviz 0.43–0.77, тёмная — 0.48–0.67.
 */
const TIERS = {
  light: [0.7, 0.58, 0.77],
  dark: [0.66, 0.53, 0.6],
} as const;
const CHROMA = { light: 0.13, dark: 0.12 } as const;

/** Цвет i-го сообщества (i — позиция в списке сообществ графа). */
export function communityColorAt(index: number): ThemedColor {
  const hue = (HUE_START + index * GOLDEN_ANGLE) % 360;
  const tier = index % 3;
  return {
    light: oklchToHex(TIERS.light[tier], CHROMA.light, hue),
    dark: oklchToHex(TIERS.dark[tier], CHROMA.dark, hue),
  };
}

/**
 * Цвета всех сообществ: детерминированно по порядку в списке (`communitySummaries` —
 * по убыванию размера, крупнейшим достаются самые далёкие друг от друга оттенки).
 * Палитра не повторяется: сколько сообществ, столько разных цветов.
 */
export function communityColors(ids: readonly string[]): Map<string, ThemedColor> {
  return new Map(ids.map((id, i) => [id, communityColorAt(i)]));
}

/** CSS-цвет для DOM: `light-dark()` сам выбирает вариант по `color-scheme` темы. */
export function themedCss(color: ThemedColor | undefined, fallback = "var(--t0)"): string {
  return color ? `light-dark(${color.light}, ${color.dark})` : fallback;
}
