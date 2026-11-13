import { contrastRatio, deltaE, hexToOklch } from "./color";
import { communityColorAt } from "./communityPalette";
import { TYPE_PALETTE_FLOOR, typeColorAt, typeColors } from "./typePalette";

const ids = (n: number) => Array.from({ length: n }, (_, i) => `T${i}`);

function minPairwise(n: number, theme: "light" | "dark"): number {
  const colors = [...typeColors(ids(n)).values()].map((c) => c[theme]);
  let min = Infinity;
  for (let i = 0; i < colors.length; i++)
    for (let j = i + 1; j < colors.length; j++) min = Math.min(min, deltaE(colors[i], colors[j]));
  return min;
}

test("цвета типов детерминированы и идут по порядку списка", () => {
  expect([...typeColors(ids(15))]).toEqual([...typeColors(ids(15))]);
  expect(typeColors(["A", "B"]).get("B")).toEqual(typeColorAt(1));
  // Длинный список продолжает короткий: новый тип в конце не перекрашивает прежние.
  expect(typeColors(ids(15)).get("T7")).toEqual(typeColors(ids(8)).get("T7"));
});

test("первый тип сохраняет прежнюю «глину» --t1", () => {
  expect(typeColorAt(0)).toEqual({ light: "#e08a6c", dark: "#d77755" });
});

test("палитра типов — своя, не копия палитры сообществ", () => {
  expect(typeColorAt(0)).not.toEqual(communityColorAt(0));
});

// 15 типов из config.yaml: попарно различимы в обеих темах (измерено 12.1 / 10.5).
test.each([
  [8, 16],
  [15, TYPE_PALETTE_FLOOR],
  [20, 7],
])("первые %i типов попарно различимы в обеих темах (ΔE ≥ %f)", (n, floor) => {
  expect(minPairwise(n, "light")).toBeGreaterThanOrEqual(floor);
  expect(minPairwise(n, "dark")).toBeGreaterThanOrEqual(floor);
});

test("полоса светлоты dataviz, порог хромы, на тёмном фоне цвет не теряется", () => {
  for (const c of typeColors(ids(15)).values()) {
    const [Ll, Cl] = hexToOklch(c.light);
    const [Ld, Cd] = hexToOklch(c.dark);
    expect(Ll).toBeGreaterThanOrEqual(0.43);
    expect(Ll).toBeLessThanOrEqual(0.775);
    expect(Ld).toBeGreaterThanOrEqual(0.48);
    expect(Ld).toBeLessThanOrEqual(0.675);
    expect(Cl).toBeGreaterThanOrEqual(0.1);
    expect(Cd).toBeGreaterThanOrEqual(0.1);
    expect(contrastRatio(c.dark, "#1f231e")).toBeGreaterThanOrEqual(2.4);
  }
});
