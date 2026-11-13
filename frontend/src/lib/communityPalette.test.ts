import { contrastRatio, deltaE, hexToOklch } from "./color";
import { communityColorAt, communityColors, themedCss } from "./communityPalette";

const ids = (n: number) => Array.from({ length: n }, (_, i) => `c${i}`);

/** Минимальное попарное ΔE (OKLab ×100) первых n цветов в теме. */
function minPairwise(n: number, theme: "light" | "dark"): number {
  const colors = [...communityColors(ids(n)).values()].map((c) => c[theme]);
  let min = Infinity;
  for (let i = 0; i < colors.length; i++)
    for (let j = i + 1; j < colors.length; j++) min = Math.min(min, deltaE(colors[i], colors[j]));
  return min;
}

test("цвета детерминированы: тот же список — те же цвета", () => {
  expect([...communityColors(ids(20))]).toEqual([...communityColors(ids(20))]);
  expect(communityColors(["x", "y"]).get("y")).toEqual(communityColorAt(1));
  // Длинный список продолжает короткий: добавленное сообщество не перекрашивает прежние.
  expect(communityColors(ids(30)).get("c7")).toEqual(communityColors(ids(8)).get("c7"));
});

test("первый цвет — тёплая глина", () => {
  const [L, C, h] = hexToOklch(communityColorAt(0).light);
  expect(L).toBeCloseTo(0.66, 2);
  expect(C).toBeGreaterThan(0.12);
  expect(Math.abs(h - 30)).toBeLessThan(2);
});

// Честный потолок max-min-палитры (см. комментарий в communityPalette.ts). Прежний золотой угол
// давал при N = 30 всего ΔE 1.6: цвета повторялись через 21 шаг.
test.each([
  [8, 12],
  [12, 10],
  [24, 7.5],
  [40, 6.5],
  [50, 5.5],
  [80, 4.2],
])("первые %i сообществ попарно различимы в обеих темах (ΔE ≥ %f)", (n, floor) => {
  expect(minPairwise(n, "light")).toBeGreaterThanOrEqual(floor);
  expect(minPairwise(n, "dark")).toBeGreaterThanOrEqual(floor);
});

test("N ≤ 24: попарно ΔE ≥ 5 для любого N (не только на контрольных точках)", () => {
  for (let n = 2; n <= 24; n++) {
    expect(minPairwise(n, "light")).toBeGreaterThanOrEqual(5);
    expect(minPairwise(n, "dark")).toBeGreaterThanOrEqual(5);
  }
});

test.each([60, 150])("все %i сообществ получают свой цвет", (n) => {
  const colors = [...communityColors(ids(n)).values()];
  expect(new Set(colors.map((c) => c.light)).size).toBe(n);
  expect(new Set(colors.map((c) => c.dark)).size).toBe(n);
});

test("светлота в полосе своей темы, на тёмном фоне цвет не теряется", () => {
  for (const c of communityColors(ids(80)).values()) {
    const [Ll] = hexToOklch(c.light);
    const [Ld] = hexToOklch(c.dark);
    expect(Ll).toBeGreaterThanOrEqual(0.43);
    expect(Ll).toBeLessThanOrEqual(0.775);
    expect(Ld).toBeGreaterThanOrEqual(0.48);
    expect(Ld).toBeLessThanOrEqual(0.675);
    expect(contrastRatio(c.dark, "#1f231e")).toBeGreaterThanOrEqual(2.4);
  }
});

test("themedCss даёт light-dark(), без цвета — нейтральный", () => {
  expect(themedCss({ light: "#111111", dark: "#eeeeee" })).toBe("light-dark(#111111, #eeeeee)");
  expect(themedCss(undefined)).toBe("var(--t0)");
});
