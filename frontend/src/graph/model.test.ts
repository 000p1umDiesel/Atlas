import { generateFixtures } from "../fixtures/generate";
import type { GraphPayload, NodeView } from "../api/types";
import {
  buildGraph,
  DEFAULT_FILTERS,
  nodeSize,
  sizeBounds,
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

describe("visibleSubgraph: фильтр связей", () => {
  const node = (id: string): NodeView => ({
    entity_id: id,
    name: id,
    type: "Person",
    mention_count: 1,
    pagerank: null,
    community: null,
    degree: 1,
  });
  const rel = (src: string, predicate: string, dst: string) => ({
    src_id: src,
    src_name: src,
    predicate,
    dst_id: dst,
    dst_name: dst,
    description: "",
    weight: 1,
    evidence: [],
  });
  const small = buildGraph({
    nodes: ["a", "b", "c", "d"].map(node),
    relations: [rel("a", "works_at", "b"), rel("a", "knows", "c"), rel("c", "works_at", "d"), rel("b", "knows", "d")],
  });

  it("predicates: [\"works_at\"] оставляет только такие связи", () => {
    const v = visibleSubgraph(small, { ...DEFAULT_FILTERS, predicates: ["works_at"] });
    expect([...v.edges].sort()).toEqual(["a|works_at|b", "c|works_at|d"]);
    expect(v.nodes.size).toBe(4);
  });
  it("hideIsolated убирает узлы, оторванные фильтром связей", () => {
    const v = visibleSubgraph(small, { ...DEFAULT_FILTERS, predicates: ["knows"], hideIsolated: false });
    expect(v.nodes.size).toBe(4);
    const lonely = buildGraph({
      nodes: ["a", "b", "c"].map(node),
      relations: [rel("a", "works_at", "b"), rel("b", "knows", "c")],
    });
    const hidden = visibleSubgraph(lonely, { ...DEFAULT_FILTERS, predicates: ["works_at"], hideIsolated: true });
    expect([...hidden.nodes].sort()).toEqual(["a", "b"]);
  });
  it("пустой список — все связи", () => {
    expect(visibleSubgraph(small, DEFAULT_FILTERS).edges.size).toBe(4);
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
  it("на маленьком графе (10 узлов) размер лежит в [7, 18]", () => {
    const ten = buildGraph({
      nodes: Array.from({ length: 10 }, (_, i) => ({
        entity_id: `n${i}`,
        name: `n${i}`,
        type: "Person",
        mention_count: i * i + 1,
        pagerank: null,
        community: null,
        degree: 1,
      })),
      relations: [],
    });
    const bounds = sizeBounds(ten);
    expect(bounds).toEqual({ min: 7, max: 18 });
    const r = sizeRange(ten, "mentions");
    const sizes = ten.mapNodes((_, attrs) => nodeSize(attrs, "mentions", r, bounds));
    for (const s of sizes) {
      expect(s).toBeGreaterThanOrEqual(7);
      expect(s).toBeLessThanOrEqual(18);
    }
    expect(Math.min(...sizes)).toBe(7);
    expect(Math.max(...sizes)).toBe(18);
    expect(nodeSize(a(5), "mentions", { min: 5, max: 5 }, bounds)).toBe(12.5);
  });
  it("с 60 узлов — прежние границы [4, 22]", () => {
    expect(sizeBounds(g)).toEqual({ min: 4, max: 22 });
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
