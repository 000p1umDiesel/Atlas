import { MultiDirectedGraph } from "graphology";
import type { GraphPayload, NodeView } from "../api/types";

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

export function buildGraph(p: GraphPayload): Graph {
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

/** Радиус узла: sqrt-шкала по диапазону значений; pagerank=null → по mentions. */
export function nodeSize(
  attrs: Pick<NodeView, "mention_count" | "pagerank" | "degree">,
  sizeBy: SizeBy,
  range: { min: number; max: number },
): number {
  const value =
    sizeBy === "pagerank" && attrs.pagerank !== null
      ? attrs.pagerank
      : sizeBy === "degree"
        ? attrs.degree
        : attrs.mention_count;
  if (range.max <= range.min) return (SIZE_MIN + SIZE_MAX) / 2;
  const t = Math.min(1, Math.max(0, (value - range.min) / (range.max - range.min)));
  return SIZE_MIN + Math.sqrt(t) * (SIZE_MAX - SIZE_MIN);
}
