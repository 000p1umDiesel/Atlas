import { MultiDirectedGraph } from "graphology";
import { fitRatio, framingRatio, neighborhood, NO_INSETS, smoothTowards, usableInsets } from "./interaction";

function sample() {
  const g = new MultiDirectedGraph();
  for (const id of ["a", "b", "c", "d", "e"]) g.addNode(id);
  g.addEdgeWithKey("a>b", "a", "b");
  g.addEdgeWithKey("c>a", "c", "a");
  g.addEdgeWithKey("a>d", "a", "d");
  g.addEdgeWithKey("b>e", "b", "e");
  return g;
}

describe("neighborhood", () => {
  it("узел, его соседи по видимым рёбрам и сами эти рёбра (в обе стороны)", () => {
    const g = sample();
    const all = { nodes: new Set(g.nodes()), edges: new Set(g.edges()) };
    const n = neighborhood(g, "a", all);
    expect(new Set(n.nodes)).toEqual(new Set(["a", "b", "c", "d"]));
    expect(new Set(n.edges)).toEqual(new Set(["a>b", "c>a", "a>d"]));
  });

  it("скрытые рёбра и узлы не попадают; невидимый узел — пусто", () => {
    const g = sample();
    const visible = { nodes: new Set(["a", "b", "c", "d"]), edges: new Set(["a>b", "c>a"]) };
    const n = neighborhood(g, "a", visible);
    expect(new Set(n.nodes)).toEqual(new Set(["a", "b", "c"]));
    expect(neighborhood(g, "e", visible)).toEqual({ nodes: [], edges: [] });
    expect(neighborhood(g, "zzz", visible)).toEqual({ nodes: [], edges: [] });
  });
});

describe("smoothTowards", () => {
  const from = { x: 0, y: 0 };
  const to = { x: 100, y: -50 };

  it("приближается к цели, не перескакивая её", () => {
    let p = from;
    let prev = Infinity;
    for (let i = 0; i < 30; i++) {
      p = smoothTowards(p, to, 16, 80);
      const d = Math.hypot(to.x - p.x, to.y - p.y);
      expect(d).toBeLessThan(prev);
      expect(p.x).toBeLessThanOrEqual(100);
      prev = d;
    }
    expect(prev).toBeLessThan(1);
  });

  it("за нулевое время не двигается, за долгое — почти в цели", () => {
    expect(smoothTowards(from, to, 0, 80)).toEqual(from);
    const far = smoothTowards(from, to, 2000, 80);
    expect(far.x).toBeCloseTo(100, 3);
  });

  it("без сглаживания (tau = 0, prefers-reduced-motion) — сразу в цель", () => {
    expect(smoothTowards(from, to, 16, 0)).toEqual(to);
  });
});

describe("framingRatio", () => {
  it("соседи помещаются в свободную половину кадра с запасом", () => {
    // Сосед в 100 px по x при ratio = 1, свободно 200 px: нужен ratio 0.5, с запасом 1.15.
    expect(framingRatio([{ x: 100, y: 0 }], { x: 200, y: 300 })).toBeCloseTo(0.575, 5);
    // Решает худшая ось.
    expect(framingRatio([{ x: 10, y: -150 }], { x: 200, y: 100 })).toBeCloseTo(1.725, 5);
  });
  it("без соседей — обычный масштаб окрестности; пределы соблюдаются", () => {
    expect(framingRatio([], { x: 200, y: 200 })).toBe(0.3);
    expect(framingRatio([{ x: 0.1, y: 0 }], { x: 200, y: 200 })).toBe(0.06);
    expect(framingRatio([{ x: 1e6, y: 0 }], { x: 200, y: 200 })).toBe(2);
    expect(framingRatio([{ x: 100, y: 0 }], { x: 0, y: 0 })).toBe(2);
  });
});

describe("fitRatio / usableInsets", () => {
  test("масштаб по более тесной оси с запасом; больше 1 — можно (отдалить)", () => {
    expect(fitRatio({ x: 1000, y: 100 }, { x: 500, y: 500 })).toBeCloseTo(2 * 1.15, 6);
    expect(fitRatio({ x: 100, y: 400 }, { x: 500, y: 200 })).toBeCloseTo(2 * 1.15, 6);
  });

  test("приближение ограничено; один узел (без размаха) — умеренный масштаб", () => {
    expect(fitRatio({ x: 10, y: 10 }, { x: 900, y: 500 })).toBe(0.15);
    expect(fitRatio({ x: 0, y: 0 }, { x: 900, y: 500 })).toBe(0.3);
    expect(fitRatio({ x: 100, y: 100 }, { x: 0, y: 500 })).toBe(1);
  });

  test("узкий холст: отступы оси, которые не оставляют места, отбрасываются", () => {
    expect(usableInsets({ top: 60, right: 332, bottom: 80, left: 292 }, 1200, 800, 36)).toEqual({
      top: 60,
      right: 332,
      bottom: 80,
      left: 292,
    });
    expect(usableInsets({ top: 60, right: 0, bottom: 300, left: 292 }, 360, 640, 36)).toEqual({
      top: 60,
      right: 0,
      bottom: 300,
      left: 0,
    });
    expect(usableInsets({ top: -5, right: NaN, bottom: 0, left: 0 }, 800, 600, 36)).toEqual(NO_INSETS);
  });
});
