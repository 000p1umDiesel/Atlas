import { MultiDirectedGraph } from "graphology";
import { neighborhood, smoothTowards } from "./interaction";

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
