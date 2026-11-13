import { contrastRatio, cvdDeltaE, deltaE, hexToOklch, oklchToHex } from "./color";

test("контраст WCAG: чёрный к белому 21:1, цвет к себе 1:1", () => {
  expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
  expect(contrastRatio("#ffffff", "#000000")).toBeCloseTo(21, 5);
  expect(contrastRatio("#6dbbe8", "#6dbbe8")).toBeCloseTo(1, 5);
});

test("OKLCH → hex и обратно сохраняет светлоту и оттенок", () => {
  const hex = oklchToHex(0.7, 0.12, 40);
  expect(hex).toMatch(/^#[0-9a-f]{6}$/);
  const [L, C, h] = hexToOklch(hex);
  expect(L).toBeCloseTo(0.7, 2);
  expect(C).toBeCloseTo(0.12, 2);
  expect(h).toBeCloseTo(40, 0);
});

test("цвет вне охвата sRGB ужимается по хроме, а не обрезается по каналам", () => {
  const hex = oklchToHex(0.9, 0.4, 145);
  const [L, C, h] = hexToOklch(hex);
  expect(L).toBeCloseTo(0.9, 2);
  expect(C).toBeLessThan(0.4);
  expect(Math.abs(h - 145)).toBeLessThan(3);
});

test("ΔE в OKLab ×100: одинаковые цвета — 0, чёрный и белый — 100", () => {
  expect(deltaE("#123456", "#123456")).toBe(0);
  expect(deltaE("#000000", "#ffffff")).toBeCloseTo(100, 0);
});

test("при дейтеранопии красный и зелёный сближаются сильнее, чем в норме", () => {
  const normal = deltaE("#d0352b", "#3c9a3c");
  expect(cvdDeltaE("#d0352b", "#3c9a3c", "deutan")).toBeLessThan(normal);
  expect(cvdDeltaE("#d0352b", "#3c9a3c", "protan")).toBeLessThan(normal);
});
