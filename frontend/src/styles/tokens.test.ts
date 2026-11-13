import { contrastRatio, cvdDeltaE, deltaE, hexToOklch } from "../lib/color";
import css from "./tokens.css?raw";

/** Значения токенов из блока: `:root {` (светлая) или `:root[data-theme="dark"] {` (тёмная). */
function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  const body = css.slice(start, css.indexOf("}", start));
  const out: Record<string, string> = {};
  for (const m of body.matchAll(/(--[\w-]+):\s*(#[0-9a-f]{6})\s*;/gi)) out[m[1]] = m[2].toLowerCase();
  return out;
}

const THEMES = {
  light: { t: tokens(":root"), band: [0.43, 0.77] },
  dark: { t: tokens(':root[data-theme="dark"]'), band: [0.48, 0.67] },
} as const;

const slots = (t: Record<string, string>) => Array.from({ length: 8 }, (_, i) => t[`--t${i + 1}`]);

describe.each(Object.entries(THEMES))("тема %s", (_name, { t, band }) => {
  test("все восемь цветов типов заданы hex", () => {
    expect(slots(t).every((c) => /^#[0-9a-f]{6}$/.test(c ?? ""))).toBe(true);
  });

  test("приглушённый текст читается на фоне и карточке (≥ 4.5:1), основной — тем более", () => {
    for (const surface of [t["--bg"], t["--card"]]) {
      expect(contrastRatio(t["--muted"], surface)).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(t["--ink"], surface)).toBeGreaterThanOrEqual(10);
    }
  });

  test("цвета типов: полоса светлоты и порог хромы dataviz", () => {
    for (const c of slots(t)) {
      const [L, C] = hexToOklch(c);
      expect(L).toBeGreaterThanOrEqual(band[0] - 0.005);
      expect(L).toBeLessThanOrEqual(band[1] + 0.005);
      expect(C).toBeGreaterThanOrEqual(0.1 - 0.002);
    }
  });

  test("соседние слоты различимы в норме (ΔE ≥ 15) и при протанопии/дейтеранопии (ΔE ≥ 8)", () => {
    const s = slots(t);
    for (let i = 0; i + 1 < s.length; i++) {
      expect(deltaE(s[i], s[i + 1])).toBeGreaterThanOrEqual(15);
      expect(cvdDeltaE(s[i], s[i + 1], "protan")).toBeGreaterThanOrEqual(8);
      expect(cvdDeltaE(s[i], s[i + 1], "deutan")).toBeGreaterThanOrEqual(8);
    }
  });
});

test("системная тёмная тема совпадает с ручной", () => {
  const media = css.slice(css.indexOf("@media (prefers-color-scheme: dark)"));
  const system = tokens.call(null, ':root:not([data-theme="light"])');
  expect(media.length).toBeGreaterThan(0);
  expect(system).toEqual(THEMES.dark.t);
});
