import { generateFixtures } from "../fixtures/generate";
import type { GraphPayload, NodeView } from "../api/types";
import {
  buildGraph,
  DEFAULT_FILTERS,
  nodeSize,
  sizeMetric,
  sizeRange,
  visibleSubgraph,
} from "./model";

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

describe("sizeRange", () => {
  const node = (id: string, m: number, p: number | null, d = 1): NodeView => ({
    entity_id: id,
    name: id,
    type: "Person",
    mention_count: m,
    pagerank: p,
    community: null,
    degree: d,
  });
  const payload = (nodes: NodeView[]): GraphPayload => ({
    nodes,
    relations: [],
    ranked_by: "pagerank",
    truncated: false,
    total_nodes: nodes.length,
  });

  it("считается по всем узлам, а не по видимым", () => {
    const small = buildGraph(payload([node("a", 2, 0.1, 3), node("b", 50, 0.9, 7), node("c", 9, 0.4, 1)]));
    expect(sizeRange(small, "mentions")).toEqual({ min: 2, max: 50 });
    expect(sizeRange(small, "degree")).toEqual({ min: 1, max: 7 });
    expect(sizeRange(small, "pagerank")).toEqual({ min: 0.1, max: 0.9 });
    expect(sizeMetric(small, "pagerank")).toBe("pagerank");
  });

  it("хотя бы один pagerank=null → весь граф по mentions", () => {
    const mixed = buildGraph(payload([node("a", 2, 0.5), node("b", 40, null), node("c", 10, 0.01)]));
    expect(sizeMetric(mixed, "pagerank")).toBe("mentions");
    expect(sizeRange(mixed, "pagerank")).toEqual({ min: 2, max: 40 });
    const range = sizeRange(mixed, "pagerank");
    const metric = sizeMetric(mixed, "pagerank");
    // узел с pagerank тоже меряется по mentions: 40 → максимум, 2 → минимум
    expect(nodeSize(mixed.getNodeAttributes("b"), metric, range)).toBe(22);
    expect(nodeSize(mixed.getNodeAttributes("a"), metric, range)).toBe(4);
  });

  it("пустой граф → {0, 0}, nodeSize без NaN", () => {
    const empty = buildGraph(payload([]));
    const range = sizeRange(empty, "pagerank");
    expect(range).toEqual({ min: 0, max: 0 });
    const s = nodeSize({ mention_count: 3, pagerank: null, degree: 0 }, "mentions", range);
    expect(Number.isNaN(s)).toBe(false);
    expect(s).toBeGreaterThanOrEqual(4);
    expect(s).toBeLessThanOrEqual(22);
  });
});
