import type { GraphPayload, NodeView } from "../api/types";
import { clearCssColorCache } from "../lib/entityColors";
import { typeColorAt } from "../lib/typePalette";
import { generateFixtures } from "../fixtures/generate";
import { buildGraph, DEFAULT_FILTERS, nodeSize, sizeMetric, sizeRange, visibleSubgraph } from "./model";
import {
  boundingSphere,
  compositionKey,
  easeInOutCubic,
  fitDistance,
  highlightMatches,
  labelCandidates,
  linkAlpha,
  reconcileNodes,
  toForceData,
  type ForceNode,
} from "./scene3d";

// Нейтральный цвет светлой темы: hex разбирается без canvas (в jsdom его нет).
const VARS: Record<string, string> = { "--t0": "#C9C3B6" };
/** Цвет i-го типа светлой темы как `rgb(...)`. */
const typeRgb = (i: number) => {
  const h = typeColorAt(i).light;
  return `rgb(${parseInt(h.slice(1, 3), 16)}, ${parseInt(h.slice(3, 5), 16)}, ${parseInt(h.slice(5, 7), 16)})`;
};

beforeEach(() => {
  for (const [k, v] of Object.entries(VARS)) document.documentElement.style.setProperty(k, v);
});

afterEach(() => {
  clearCssColorCache();
  document.documentElement.removeAttribute("style");
});

const node = (id: string, type: string, m: number, p: number | null, c: string | null = null): NodeView => ({
  entity_id: id,
  name: `Имя ${id}`,
  type,
  mention_count: m,
  pagerank: p,
  community: c,
  degree: 1,
});

function small() {
  const payload: GraphPayload = {
    nodes: [
      node("a", "Person", 2, 0.1, "c1"),
      node("b", "Org", 50, 0.9, "c2"),
      node("c", "Person", 9, 0.4, "c1"),
      node("d", "Place", 1, 0.05),
    ],
    relations: [
      { src_id: "a", src_name: "", predicate: "knows", dst_id: "b", dst_name: "", description: "", weight: 2, evidence: [] },
      { src_id: "b", src_name: "", predicate: "owns", dst_id: "a", dst_name: "", description: "", weight: 5, evidence: [] },
      { src_id: "b", src_name: "", predicate: "near", dst_id: "c", dst_name: "", description: "", weight: 1, evidence: [] },
      { src_id: "c", src_name: "", predicate: "in", dst_id: "d", dst_name: "", description: "", weight: 3, evidence: [] },
    ],
    ranked_by: "pagerank",
    truncated: false,
    total_nodes: 4,
  };
  return buildGraph(payload);
}

const COLOR = /^(#[0-9a-f]{6}|rgb\(\d+, \d+, \d+\))$/i;

describe("toForceData", () => {
  it("берёт только видимые узлы и рёбра между ними", () => {
    const g = small();
    const filters = { ...DEFAULT_FILTERS, types: ["Person", "Org"] };
    const data = toForceData(g, visibleSubgraph(g, filters), filters);
    expect(data.nodes.map((n) => n.id).sort()).toEqual(["a", "b", "c"]);
    for (const l of data.links) {
      expect(["a", "b", "c"]).toContain(l.source);
      expect(["a", "b", "c"]).toContain(l.target);
    }
    expect(data.links.some((l) => l.source === "d" || l.target === "d")).toBe(false);
  });

  it("параллельные рёбра пары сливаются в одну связь с максимальным весом", () => {
    const g = small();
    const data = toForceData(g, visibleSubgraph(g, DEFAULT_FILTERS), DEFAULT_FILTERS);
    const ab = data.links.filter((l) => [l.source, l.target].sort().join() === "a,b");
    expect(ab).toHaveLength(1);
    expect(ab[0]!.weight).toBe(5);
    expect(data.links).toHaveLength(3);
  });

  it("val = nodeSize по эффективной метрике и диапазону всего графа", () => {
    const g = small();
    for (const sizeBy of ["pagerank", "mentions", "degree"] as const) {
      const filters = { ...DEFAULT_FILTERS, sizeBy, types: ["Person"] };
      const data = toForceData(g, visibleSubgraph(g, filters), filters);
      const metric = sizeMetric(g, sizeBy);
      const range = sizeRange(g, sizeBy);
      for (const n of data.nodes) {
        expect(n.val).toBe(nodeSize(g.getNodeAttributes(n.id), metric, range));
      }
    }
  });

  it("цвета — конкретные строки, не var(...), по типу и по сообществу", () => {
    const g = small();
    const byType = toForceData(g, visibleSubgraph(g, DEFAULT_FILTERS), DEFAULT_FILTERS, ["Person", "Org"]);
    for (const n of byType.nodes) {
      expect(n.color).toMatch(COLOR);
      expect(n.color).not.toContain("var(");
    }
    const color = (id: string, data = byType) => data.nodes.find((n) => n.id === id)!.color;
    expect(color("a")).toBe(typeRgb(0)); // Person — первый тип
    expect(color("b")).toBe(typeRgb(1)); // Org — второй
    expect(color("d")).toBe("rgb(201, 195, 182)"); // не настроен → --t0

    const f = { ...DEFAULT_FILTERS, colorBy: "community" as const };
    const byCommunity = toForceData(g, visibleSubgraph(g, f), f, ["Person", "Org"]);
    for (const n of byCommunity.nodes) expect(n.color).toMatch(COLOR);
    expect(color("a", byCommunity)).toBe(color("c", byCommunity));
    expect(color("d", byCommunity)).toBe("rgb(201, 195, 182)");
  });

  it("без списка типов цвета идут по отсортированным типам графа", () => {
    const g = small();
    const data = toForceData(g, visibleSubgraph(g, DEFAULT_FILTERS), DEFAULT_FILTERS);
    // Org, Person, Place → цвета 0, 1, 2
    expect(data.nodes.find((n) => n.id === "b")!.color).toBe(typeRgb(0));
    expect(data.nodes.find((n) => n.id === "d")!.color).toBe(typeRgb(2));
  });

  it("имя узла — исходный текст; пустой видимый набор → пустые массивы", () => {
    const g = small();
    const data = toForceData(g, visibleSubgraph(g, DEFAULT_FILTERS), DEFAULT_FILTERS);
    expect(data.nodes.find((n) => n.id === "a")!.name).toBe("Имя a");
    const none = { ...DEFAULT_FILTERS, types: ["Nope"] };
    expect(toForceData(g, visibleSubgraph(g, none), none)).toEqual({ nodes: [], links: [] });
  });

  it("на фикстурах: 300 узлов, все цвета конкретные", () => {
    const g = buildGraph(generateFixtures(7).graph);
    const data = toForceData(g, visibleSubgraph(g, DEFAULT_FILTERS), DEFAULT_FILTERS);
    expect(data.nodes).toHaveLength(300);
    expect(data.nodes.every((n) => COLOR.test(n.color))).toBe(true);
    expect(data.links.length).toBeGreaterThan(0);
  });
});

const fn = (id: string, val: number, name = id): ForceNode => ({ id, name, val, color: "rgb(0, 0, 0)" });

describe("labelCandidates", () => {
  it("топ-N по размеру плюс обязательные", () => {
    const nodes = Array.from({ length: 50 }, (_, i) => fn(`n${i}`, i));
    const set = labelCandidates(nodes, 30, ["n0", "missing"]);
    expect(set.size).toBe(31);
    for (let i = 20; i < 50; i++) expect(set.has(`n${i}`)).toBe(true);
    expect(set.has("n19")).toBe(false);
    expect(set.has("n0")).toBe(true);
    expect(set.has("missing")).toBe(false);
  });

  it("равные размеры упорядочены детерминированно по id", () => {
    const nodes = [fn("c", 1), fn("a", 1), fn("b", 1)];
    expect([...labelCandidates(nodes, 2)].sort()).toEqual(["a", "b"]);
  });
});

describe("highlightMatches", () => {
  it("подстрока без учёта регистра; пустая строка — ничего", () => {
    const nodes = [fn("1", 1, "Москва"), fn("2", 1, "Moscow State"), fn("3", 1, "Париж")];
    expect([...highlightMatches(nodes, "  моск ")]).toEqual(["1"]);
    expect([...highlightMatches(nodes, "MOSCOW")]).toEqual(["2"]);
    expect(highlightMatches(nodes, "   ").size).toBe(0);
  });
});

describe("boundingSphere / fitDistance", () => {
  it("охватывает все точки с учётом радиусов", () => {
    const s = boundingSphere([
      { x: -10, y: 0, z: 0, r: 1 },
      { x: 10, y: 0, z: 0, r: 2 },
      { x: 0, y: 5, z: 0, r: 0 },
    ])!;
    expect(s.center).toEqual({ x: 0, y: 2.5, z: 0 });
    for (const p of [
      { x: -10, y: 0, z: 0, r: 1 },
      { x: 10, y: 0, z: 0, r: 2 },
    ]) {
      const d = Math.hypot(p.x - s.center.x, p.y - s.center.y, p.z - s.center.z) + p.r;
      expect(s.radius).toBeGreaterThanOrEqual(d - 1e-9);
    }
  });

  it("пропускает нечисловые координаты; пусто → null", () => {
    expect(boundingSphere([])).toBeNull();
    expect(boundingSphere([{ x: Number.NaN, y: 0, z: 0, r: 1 }])).toBeNull();
    const one = boundingSphere([{ x: 3, y: 4, z: 5, r: 0 }])!;
    expect(one.center).toEqual({ x: 3, y: 4, z: 5 });
    expect(one.radius).toBeGreaterThan(0);
  });

  it("дистанция вписывает сферу по узкой стороне", () => {
    const tall = fitDistance(10, 50, 0.5);
    const wide = fitDistance(10, 50, 2);
    expect(tall).toBeGreaterThan(wide);
    const half = (50 / 2) * (Math.PI / 180);
    expect(wide).toBeCloseTo(10 / Math.sin(half), 6);
  });
});

describe("reconcileNodes", () => {
  it("сохраняет объекты и позиции оставшихся узлов, обновляет атрибуты", () => {
    const old = { ...fn("a", 4), x: 1, y: 2, z: 3, vx: 0.5 };
    const prev = new Map<string, ForceNode>([["a", old]]);
    const next = [{ ...fn("a", 9, "Новое"), color: "rgb(1, 2, 3)" }, fn("b", 5)];
    const out = reconcileNodes(prev, next);
    expect(out[0]).toBe(old);
    expect(out[0]).toMatchObject({ id: "a", val: 9, name: "Новое", color: "rgb(1, 2, 3)", x: 1, y: 2, z: 3 });
    expect(out[1]).toBe(next[1]);
  });
});

describe("compositionKey", () => {
  it("зависит только от состава, не от цвета и размера", () => {
    const base = { nodes: [fn("a", 1), fn("b", 2)], links: [{ source: "a", target: "b", weight: 1 }] };
    const recolored = {
      nodes: [{ ...fn("b", 7), color: "rgb(9, 9, 9)" }, fn("a", 3)],
      links: [{ source: "a", target: "b", weight: 4 }],
    };
    expect(compositionKey(recolored)).toBe(compositionKey(base));
    expect(compositionKey({ nodes: base.nodes, links: [] })).not.toBe(compositionKey(base));
    expect(compositionKey({ nodes: [fn("a", 1)], links: [] })).not.toBe(
      compositionKey({ nodes: base.nodes, links: [] }),
    );
  });
});

describe("linkAlpha / easeInOutCubic", () => {
  it("прозрачность растёт с весом, ступенями, в [0.2, 0.75]", () => {
    const levels = new Set<number>();
    let prev = 0;
    for (let w = 0; w <= 10; w += 0.25) {
      const a = linkAlpha(w, 10);
      expect(a).toBeGreaterThanOrEqual(prev);
      expect(a).toBeGreaterThanOrEqual(0.2);
      expect(a).toBeLessThanOrEqual(0.75);
      levels.add(a);
      prev = a;
    }
    expect(levels.size).toBeLessThanOrEqual(6);
    expect(linkAlpha(5, 0)).toBe(0.2);
    expect(linkAlpha(Number.NaN, 10)).toBe(0.2);
  });

  it("easing: концы 0 и 1, середина 0.5, монотонно", () => {
    expect(easeInOutCubic(0)).toBe(0);
    expect(easeInOutCubic(1)).toBe(1);
    expect(easeInOutCubic(0.5)).toBeCloseTo(0.5, 9);
    expect(easeInOutCubic(-1)).toBe(0);
    expect(easeInOutCubic(2)).toBe(1);
    expect(easeInOutCubic(0.25)).toBeLessThan(0.25);
  });
});
