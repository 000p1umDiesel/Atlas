import { generateFixtures } from "../fixtures/generate";
import { buildGraph, DEFAULT_FILTERS, nodeSize, visibleSubgraph } from "./model";

const g = buildGraph(generateFixtures(7).graph);

describe("visibleSubgraph", () => {
  it("по умолчанию видны все узлы", () => {
    expect(visibleSubgraph(g, DEFAULT_FILTERS).nodes.size).toBe(300);
  });
  it("тип без узлов даёт пустые множества", () => {
    const v = visibleSubgraph(g, { ...DEFAULT_FILTERS, types: ["Nope"] });
    expect(v.nodes.size).toBe(0);
    expect(v.edges.size).toBe(0);
  });
  it("minWeight выше максимума убирает рёбра и изолированные", () => {
    const f = { ...DEFAULT_FILTERS, minWeight: 99 };
    expect(visibleSubgraph(g, f).edges.size).toBe(0);
    expect(visibleSubgraph(g, { ...f, hideIsolated: true }).nodes.size).toBe(0);
  });
  it("фильтр типов убирает рёбра к скрытым узлам", () => {
    const v = visibleSubgraph(g, { ...DEFAULT_FILTERS, types: ["Person"] });
    for (const e of v.edges) {
      expect(v.nodes.has(g.source(e))).toBe(true);
      expect(v.nodes.has(g.target(e))).toBe(true);
    }
  });
  it("фильтр по сообществу и mentions", () => {
    const c = g.getNodeAttribute(g.nodes()[0], "community");
    const v = visibleSubgraph(g, { ...DEFAULT_FILTERS, community: c });
    for (const n of v.nodes) expect(g.getNodeAttribute(n, "community")).toBe(c);
    expect(visibleSubgraph(g, { ...DEFAULT_FILTERS, minMentions: 1e9 }).nodes.size).toBe(0);
  });
});

describe("nodeSize", () => {
  const range = { min: 1, max: 100 };
  const a = (m: number, p: number | null = null) => ({ mention_count: m, pagerank: p, degree: m });
  it("монотонна и в [4,22]", () => {
    let prev = 0;
    for (const m of [1, 5, 20, 60, 100]) {
      const s = nodeSize(a(m), "mentions", range);
      expect(s).toBeGreaterThanOrEqual(prev);
      expect(s).toBeGreaterThanOrEqual(4);
      expect(s).toBeLessThanOrEqual(22);
      prev = s;
    }
    expect(nodeSize(a(1), "mentions", range)).toBe(4);
    expect(nodeSize(a(100), "mentions", range)).toBe(22);
  });
  it("равные min/max дают середину", () => {
    expect(nodeSize(a(5), "mentions", { min: 5, max: 5 })).toBe(13);
  });
  it("pagerank null откатывается на mentions", () => {
    expect(nodeSize(a(100, null), "pagerank", range)).toBe(22);
  });
});
