import { contrastRatio, deltaE, hexToOklch } from "./color";
import { communityColorAt, communityColors, themedCss } from "./communityPalette";

const ids = (n: number) => Array.from({ length: n }, (_, i) => `c${i}`);

test("цвета детерминированы: тот же список — те же цвета", () => {
  expect([...communityColors(ids(20))]).toEqual([...communityColors(ids(20))]);
  expect(communityColors(["x", "y"]).get("y")).toEqual(communityColorAt(1));
});

test.each([8, 24, 60, 150])("все %i сообществ получают разные цвета в обеих темах", (n) => {
  const colors = [...communityColors(ids(n)).values()];
  expect(new Set(colors.map((c) => c.light)).size).toBe(n);
  expect(new Set(colors.map((c) => c.dark)).size).toBe(n);
});

test("соседи по списку заметно различаются (ΔE ≥ 15) на любом N", () => {
  const colors = [...communityColors(ids(80)).values()];
  for (let i = 0; i + 1 < colors.length; i++) {
    expect(deltaE(colors[i].light, colors[i + 1].light)).toBeGreaterThanOrEqual(15);
    expect(deltaE(colors[i].dark, colors[i + 1].dark)).toBeGreaterThanOrEqual(15);
  }
});

// Все пары из восьми не дотягивают до 15 — это предел dataviz для «все со всеми» (≈3 цвета);
// держим порог CVD-цели 8, а соседей по списку — 15 (тест выше).
test("первые 8 сообществ попарно различимы (ΔE ≥ 8)", () => {
  const colors = [...communityColors(ids(8)).values()];
  for (let i = 0; i < colors.length; i++)
    for (let j = i + 1; j < colors.length; j++) {
      expect(deltaE(colors[i].light, colors[j].light)).toBeGreaterThanOrEqual(8);
      expect(deltaE(colors[i].dark, colors[j].dark)).toBeGreaterThanOrEqual(8);
    }
});

test("светлота и хрома в полосе своей темы, на фоне цвет не теряется", () => {
  for (const c of communityColors(ids(40)).values()) {
    const [Ll, Cl] = hexToOklch(c.light);
    const [Ld, Cd] = hexToOklch(c.dark);
    expect(Ll).toBeGreaterThanOrEqual(0.43);
    expect(Ll).toBeLessThanOrEqual(0.775);
    expect(Ld).toBeGreaterThanOrEqual(0.48);
    expect(Ld).toBeLessThanOrEqual(0.675);
    expect(Cl).toBeGreaterThanOrEqual(0.09);
    expect(Cd).toBeGreaterThanOrEqual(0.09);
    expect(contrastRatio(c.dark, "#1f231e")).toBeGreaterThanOrEqual(2.5);
  }
});

test("themedCss даёт light-dark(), без цвета — нейтральный", () => {
  expect(themedCss({ light: "#111111", dark: "#eeeeee" })).toBe("light-dark(#111111, #eeeeee)");
  expect(themedCss(undefined)).toBe("var(--t0)");
});
