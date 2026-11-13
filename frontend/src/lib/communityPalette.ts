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
const CHROMAS = [0.08, 0.13, 0.17];
/** В тёмной теме чуть меньше хромы: насыщенный цвет на тёмном фоне «светится». */
const DARK_CHROMA = 0.92;
/** Первый цвет — тёплая глина, как первый тип. */
const ANCHOR = { hue: 30, tier: 2, chroma: 0.13 };

interface Candidate {
  color: ThemedColor;
  light: [number, number, number];
  dark: [number, number, number];
}

let candidates: Candidate[] | null = null;
/** Уже выбранная последовательность (индексы кандидатов) и расстояние каждого кандидата до неё. */
const sequence: number[] = [];
let nearest: Float64Array | null = null;

function buildCandidates(): Candidate[] {
  const out: Candidate[] = [];
  for (let hue = 0; hue < 360; hue += HUE_STEP) {
    for (let t = 0; t < TIERS_LIGHT.length; t++) {
      for (const c of CHROMAS) {
        const light = oklchToHex(TIERS_LIGHT[t], c, hue);
        const dark = oklchToHex(TIERS_DARK[t], c * DARK_CHROMA, hue);
        out.push({ color: { light, dark }, light: hexToOklab(light), dark: hexToOklab(dark) });
      }
    }
  }
  return out;
}

const dist = (a: number[], b: number[]) => 100 * Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

/** Дописать последовательность до `n` цветов (детерминированно, при равенстве — меньший индекс). */
function extend(n: number) {
  if (!candidates) {
    candidates = buildCandidates();
    nearest = new Float64Array(candidates.length).fill(Infinity);
  }
  const all = candidates;
  const near = nearest as Float64Array;
  while (sequence.length < Math.min(n, all.length)) {
    let pick: number;
    if (sequence.length === 0) {
      const tier = ANCHOR.tier;
      pick = all.findIndex(
        (_, i) =>
          Math.floor(i / (TIERS_LIGHT.length * CHROMAS.length)) * HUE_STEP === ANCHOR.hue &&
          Math.floor(i / CHROMAS.length) % TIERS_LIGHT.length === tier &&
          CHROMAS[i % CHROMAS.length] === ANCHOR.chroma,
      );
    } else {
      pick = 0;
      for (let i = 1; i < all.length; i++) if (near[i] > near[pick]) pick = i;
    }
    sequence.push(pick);
    const p = all[pick];
    for (let i = 0; i < all.length; i++) {
      const d = Math.min(dist(all[i].light, p.light), dist(all[i].dark, p.dark));
      if (d < near[i]) near[i] = d;
    }
  }
}

/** Цвет i-го сообщества (i — позиция в списке сообществ графа). */
export function communityColorAt(index: number): ThemedColor {
  extend(index + 1);
  const all = candidates as Candidate[];
  // Сетка исчерпана (больше 864 сообществ) — цвета пойдут по кругу.
  return all[sequence[index % sequence.length]].color;
}

/**
 * Цвета всех сообществ: детерминированно по порядку в списке (`communitySummaries` —
 * по убыванию размера, затем по id): крупнейшим достаются самые далёкие друг от друга цвета,
 * и пока список тот же, цвета не меняются. Каждое сообщество получает свой цвет.
 */
export function communityColors(ids: readonly string[]): Map<string, ThemedColor> {
  extend(ids.length);
  return new Map(ids.map((id, i) => [id, communityColorAt(i)]));
}

/** CSS-цвет для DOM: `light-dark()` сам выбирает вариант по `color-scheme` темы. */
export function themedCss(color: ThemedColor | undefined, fallback = "var(--t0)"): string {
  return color ? `light-dark(${color.light}, ${color.dark})` : fallback;
}
