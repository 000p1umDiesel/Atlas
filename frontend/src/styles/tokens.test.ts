import { contrastRatio } from "../lib/color";
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
  light: { t: tokens(":root") },
  dark: { t: tokens(':root[data-theme="dark"]') },
} as const;

describe.each(Object.entries(THEMES))("тема %s", (_name, { t }) => {
  test("нейтральный цвет --t0 задан hex; цвета типов — не токены (их строит генератор)", () => {
    expect(t["--t0"]).toMatch(/^#[0-9a-f]{6}$/);
    expect(Object.keys(t).filter((k) => /^--t[1-9]/.test(k))).toEqual([]);
  });

  test("приглушённый текст читается на фоне и карточке (≥ 4.5:1), основной — тем более", () => {
    for (const surface of [t["--bg"], t["--card"]]) {
      expect(contrastRatio(t["--muted"], surface)).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(t["--ink"], surface)).toBeGreaterThanOrEqual(10);
    }
  });
});

test("системная тёмная тема совпадает с ручной", () => {
  const media = css.slice(css.indexOf("@media (prefers-color-scheme: dark)"));
  const system = tokens.call(null, ':root:not([data-theme="light"])');
  expect(media.length).toBeGreaterThan(0);
  expect(system).toEqual(THEMES.dark.t);
});
