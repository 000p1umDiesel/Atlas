import { contrastRatio, cvdDeltaE, deltaE, hexToOklch } from "./color";
import { communityColorAt } from "./communityPalette";
import {
  TYPE_ADJACENT_CVD_FLOOR,
  TYPE_PALETTE_FLOOR,
  typeColorAt,
  typeColorIndex,
  typeColors,
} from "./typePalette";

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

// 18 типов config.yaml, из них 17 цветных (Other — нейтральный): измерено 9.2 / 8.9.
test.each([
  [8, 16],
  [15, 10],
  [17, TYPE_PALETTE_FLOOR],
  [20, 7],
])("первые %i типов попарно различимы в обеих темах (ΔE ≥ %f)", (n, floor) => {
  expect(minPairwise(n, "light")).toBeGreaterThanOrEqual(floor);
  expect(minPairwise(n, "dark")).toBeGreaterThanOrEqual(floor);
});

test.each(["light", "dark"] as const)(
  "соседние цвета 17 типов при протанопии/дейтеранопии не хуже измеренного (%s)",
  (theme) => {
    const colors = Array.from({ length: 17 }, (_, i) => typeColorAt(i)[theme]);
    for (let i = 0; i + 1 < colors.length; i++) {
      expect(cvdDeltaE(colors[i], colors[i + 1], "protan")).toBeGreaterThanOrEqual(TYPE_ADJACENT_CVD_FLOOR[theme]);
      expect(cvdDeltaE(colors[i], colors[i + 1], "deutan")).toBeGreaterThanOrEqual(TYPE_ADJACENT_CVD_FLOOR[theme]);
    }
  },
);

test("«Другое» нейтральное и слот палитры не занимает", () => {
  const order = ["Person", "Other", "Organization"];
  expect(typeColorIndex("Other", order)).toBe(-1);
  expect(typeColorIndex("Organization", order)).toBe(1);
  expect(typeColorIndex("Nope", order)).toBe(-1);
  const map = typeColors(order);
  expect(map.has("Other")).toBe(false);
  expect(map.get("Organization")).toEqual(typeColorAt(1));
});

test("полоса светлоты dataviz, порог хромы, на тёмном фоне цвет не теряется", () => {
  for (const c of typeColors(ids(17)).values()) {
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
