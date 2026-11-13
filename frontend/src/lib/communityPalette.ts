import { hexToOklab, oklchToHex } from "./color";

export interface ThemedColor {
  light: string;
  dark: string;
}

/*
 * Палитра сообществ: жадный выбор «самый далёкий от уже выбранных» (max-min по ΔE в OKLab)
 * из сетки оттенок × светлота × хрома. Каждый кандидат — пара цветов (светлая и тёмная тема)
 * одного оттенка; расстояние кандидата — худшее из двух тем, так что различимы обе.
 *
 * Честный потолок (минимальное попарное ΔE OKLab×100, светлая/тёмная; проверяется тестом):
 *   N = 8 → 14.8/14.5, 12 → 12.7/11.8, 20 → 9.3/9.0, 24 → 8.1/8.1, 30 → 8.0/7.8,
 *   40 → 6.9/6.7, 50 → 6.5/6.1, 80 → 4.8/4.8. Больше — цвета всё ещё разные, но соседние оттенки уже близки:
 *   там сообщество различают по легенде и подписи, а не только по цвету.
 * Прежняя схема (золотой угол и три яруса) повторялась через 21 шаг: при N = 30 было ΔE 1.6.
 */

const HUE_STEP = 5;
/** Ярусы светлоты: светлая тема в полосе dataviz 0.43–0.77, тёмная — 0.48–0.67. */
const TIERS_LIGHT = [0.5, 0.58, 0.66, 0.74];
const TIERS_DARK = [0.5, 0.555, 0.61, 0.665];
/** В тёмной теме чуть меньше хромы: насыщенный цвет на тёмном фоне «светится». */
const DARK_CHROMA = 0.92;

interface Candidate {
  color: ThemedColor;
  light: [number, number, number];
  dark: [number, number, number];
}

const candidate = (color: ThemedColor): Candidate => ({ color, light: hexToOklab(color.light), dark: hexToOklab(color.dark) });

const dist = (a: number[], b: number[]) => 100 * Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

/**
 * Различимая палитра: первый цвет — `seed`, дальше жадно «самый далёкий от уже выбранных»
 * (max-min по ΔE OKLab, худшее из двух тем) из сетки оттенок × светлота × `chromas`.
 * Последовательность дописывается лениво и не меняется: i-й цвет всегда один и тот же.
 */
export function createDistinctPalette(opts: {
  chromas: readonly number[];
  seed: ThemedColor;
  /** Отбросить кандидатов, у которых хрома после ужатия в охват sRGB ниже порога (светлая тема). */
  minChroma?: number;
}): (index: number) => ThemedColor {
  let candidates: Candidate[] | null = null;
  let nearest: Float64Array | null = null;
  const sequence: Candidate[] = [];

  const take = (p: Candidate) => {
    sequence.push(p);
    const all = candidates as Candidate[];
    const near = nearest as Float64Array;
    for (let i = 0; i < all.length; i++) {
      const d = Math.min(dist(all[i].light, p.light), dist(all[i].dark, p.dark));
      if (d < near[i]) near[i] = d;
    }
  };

  const extend = (n: number) => {
    if (!candidates) {
      candidates = [];
      for (let hue = 0; hue < 360; hue += HUE_STEP)
        for (let t = 0; t < TIERS_LIGHT.length; t++)
          for (const c of opts.chromas) {
            const next = candidate({
              light: oklchToHex(TIERS_LIGHT[t], c, hue),
              dark: oklchToHex(TIERS_DARK[t], c * DARK_CHROMA, hue),
            });
            if (opts.minChroma !== undefined && Math.hypot(next.light[1], next.light[2]) < opts.minChroma) continue;
            candidates.push(next);
          }
      nearest = new Float64Array(candidates.length).fill(Infinity);
      take(candidate(opts.seed));
    }
    const all = candidates;
    const near = nearest as Float64Array;
    // Сетка исчерпана (кандидаты на нулевом расстоянии) — дальше цвета пойдут по кругу.
    while (sequence.length < n) {
      let pick = 0;
      for (let i = 1; i < all.length; i++) if (near[i] > near[pick]) pick = i;
      if (near[pick] <= 0) break;
      take(all[pick]);
    }
  };

  return (index: number) => {
    extend(index + 1);
    return sequence[index % sequence.length].color;
  };
}

/** Сообщества: первый цвет — тёплая глина (оттенок 30°, третий ярус, хрома 0.13). */
const communityPalette = createDistinctPalette({
  chromas: [0.08, 0.13, 0.17],
  seed: { light: oklchToHex(TIERS_LIGHT[2], 0.13, 30), dark: oklchToHex(TIERS_DARK[2], 0.13 * DARK_CHROMA, 30) },
});

/** Цвет i-го сообщества (i — позиция в списке сообществ графа). */
export function communityColorAt(index: number): ThemedColor {
  return communityPalette(index);
}

/**
 * Цвета всех сообществ: детерминированно по порядку в списке (`communitySummaries` —
 * по убыванию размера, затем по id): крупнейшим достаются самые далёкие друг от друга цвета,
 * и пока список тот же, цвета не меняются. Каждое сообщество получает свой цвет.
 */
export function communityColors(ids: readonly string[]): Map<string, ThemedColor> {
  return new Map(ids.map((id, i) => [id, communityColorAt(i)]));
}

/** CSS-цвет для DOM: `light-dark()` сам выбирает вариант по `color-scheme` темы. */
export function themedCss(color: ThemedColor | undefined, fallback = "var(--t0)"): string {
  return color ? `light-dark(${color.light}, ${color.dark})` : fallback;
}
