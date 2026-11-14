import { MultiDirectedGraph } from "graphology";
import type { GraphPart, NodeView } from "../api/types";

export type ColorBy = "type" | "community";
export type SizeBy = "pagerank" | "mentions" | "degree";

export interface GraphFilters {
  types: string[];
  community: string | null;
  minWeight: number;
  minMentions: number;
  hideIsolated: boolean;
  colorBy: ColorBy;
  sizeBy: SizeBy;
  highlight: string;
}

export const DEFAULT_FILTERS: GraphFilters = {
  types: [],
  community: null,
  minWeight: 0,
  minMentions: 0,
  hideIsolated: false,
  colorBy: "type",
  sizeBy: "pagerank",
  highlight: "",
};

type EdgeAttrs = {
  predicate: string;
  description: string;
  weight: number;
  evidence: string[];
};

export type Graph = MultiDirectedGraph<NodeView, EdgeAttrs>;

export function buildGraph(p: GraphPart): Graph {
  const g: Graph = new MultiDirectedGraph();
  for (const n of p.nodes) g.addNode(n.entity_id, { ...n });
  for (const r of p.relations) {
    if (!g.hasNode(r.src_id) || !g.hasNode(r.dst_id)) continue;
    const key = `${r.src_id}|${r.predicate}|${r.dst_id}`;
    if (g.hasEdge(key)) continue;
    g.addEdgeWithKey(key, r.src_id, r.dst_id, {
      predicate: r.predicate,
      description: r.description,
      weight: r.weight,
      evidence: r.evidence,
    });
  }
  return g;
}

export function visibleSubgraph(
  g: Graph,
  f: GraphFilters,
): { nodes: Set<string>; edges: Set<string> } {
  const typeSet = new Set(f.types);
  const nodes = new Set<string>();
  g.forEachNode((id, a) => {
    if (typeSet.size > 0 && !typeSet.has(a.type)) return;
    if (f.community !== null && a.community !== f.community) return;
    if (a.mention_count < f.minMentions) return;
    nodes.add(id);
  });
  const edges = new Set<string>();
  const connected = new Set<string>();
  g.forEachEdge((key, a, s, t) => {
    if (a.weight < f.minWeight) return;
    if (!nodes.has(s) || !nodes.has(t)) return;
    edges.add(key);
    connected.add(s);
    connected.add(t);
  });
  if (f.hideIsolated) {
    for (const id of [...nodes]) if (!connected.has(id)) nodes.delete(id);
  }
  return { nodes, edges };
}

const SIZE_MIN = 4;
const SIZE_MAX = 22;

type SizeAttrs = Pick<NodeView, "mention_count" | "pagerank" | "degree">;

function metricValue(attrs: SizeAttrs, metric: SizeBy): number {
  if (metric === "pagerank" && attrs.pagerank !== null) return attrs.pagerank;
  if (metric === "degree") return attrs.degree;
  return attrs.mention_count;
}

/**
 * Метрика размера, которая реально применяется ко всему графу: если хоть у одного
 * узла нет pagerank (анализ не запускался или устарел), весь граф меряется по mentions —
 * иначе шкалы pagerank и mentions смешались бы.
 */
export function sizeMetric(g: Graph, sizeBy: SizeBy): SizeBy {
  if (sizeBy !== "pagerank") return sizeBy;
  return g.someNode((_, a) => a.pagerank === null) ? "mentions" : "pagerank";
}

/** Диапазон метрики по ВСЕМ узлам (размеры не прыгают при фильтрации); пустой граф → {0, 0}. */
export function sizeRange(g: Graph, sizeBy: SizeBy): { min: number; max: number } {
  const metric = sizeMetric(g, sizeBy);
  let min = Infinity;
  let max = -Infinity;
  g.forEachNode((_, a) => {
    const v = metricValue(a, metric);
    if (!Number.isFinite(v)) return;
    if (v < min) min = v;
    if (v > max) max = v;
  });
  return min <= max ? { min, max } : { min: 0, max: 0 };
}

/**
 * Радиус узла: sqrt-шкала по диапазону значений. `sizeBy` — результат `sizeMetric(g, …)`,
 * `range` — `sizeRange(g, …)`; pagerank=null у отдельного узла → по mentions.
 */
export function nodeSize(attrs: SizeAttrs, sizeBy: SizeBy, range: { min: number; max: number }): number {
  const value = metricValue(attrs, sizeBy);
  if (!Number.isFinite(value) || !(range.max > range.min)) return (SIZE_MIN + SIZE_MAX) / 2;
  const t = Math.min(1, Math.max(0, (value - range.min) / (range.max - range.min)));
  return SIZE_MIN + Math.sqrt(t) * (SIZE_MAX - SIZE_MIN);
}
