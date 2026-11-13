// Чистые части взаимодействия с графом: окрестность узла и плавное следование за курсором.
import type { AbstractGraph } from "graphology-types";

export interface Visible {
  nodes: ReadonlySet<string>;
  edges: ReadonlySet<string>;
}

/**
 * Окрестность на один шаг: сам узел, соседи по видимым рёбрам (входящим и исходящим)
 * и эти рёбра. Невидимый или несуществующий узел — пустая окрестность.
 */
export function neighborhood(
  graph: AbstractGraph,
  node: string,
  visible: Visible,
): { nodes: string[]; edges: string[] } {
  if (!graph.hasNode(node) || !visible.nodes.has(node)) return { nodes: [], edges: [] };
  const nodes = new Set([node]);
  const edges: string[] = [];
  graph.forEachEdge(node, (edge, _a, src, dst) => {
    if (!visible.edges.has(edge)) return;
    const other = src === node ? dst : src;
    if (!visible.nodes.has(other)) return;
    edges.push(edge);
    nodes.add(other);
  });
  return { nodes: [...nodes], edges };
}

export interface Point {
  x: number;
  y: number;
}

/**
 * Шаг экспоненциального сглаживания к цели: за `dtMs` проходится доля `1 − e^(−dt/τ)`
 * оставшегося пути. Не перескакивает цель и не зависит от частоты кадров; τ = 0 — сразу в цель.
 */
export function smoothTowards(current: Point, target: Point, dtMs: number, tauMs: number): Point {
  if (!(tauMs > 0)) return { x: target.x, y: target.y };
  if (!(dtMs > 0)) return { x: current.x, y: current.y };
  const k = 1 - Math.exp(-dtMs / tauMs);
  return { x: current.x + (target.x - current.x) * k, y: current.y + (target.y - current.y) * k };
}
