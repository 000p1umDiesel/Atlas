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

/** Масштаб камеры для окрестности: без соседей, пределы (Sigma: меньше — ближе). */
const FRAME_EMPTY = 0.3;
const FRAME_MIN = 0.06;
const FRAME_MAX = 2;
const FRAME_MARGIN = 1.15;

/**
 * Масштаб камеры Sigma (`ratio`), при котором все соседи узла видны: `offsets` — их смещения
 * от узла в пикселях экрана при ratio = 1, `half` — свободная полуширина и полувысота кадра
 * вокруг узла (за вычетом карточки). На экране смещение = offset / ratio.
 */
export function framingRatio(offsets: readonly Point[], half: Point): number {
  if (offsets.length === 0) return FRAME_EMPTY;
  if (!(half.x > 0) || !(half.y > 0)) return FRAME_MAX;
  let need = 0;
  for (const o of offsets) need = Math.max(need, Math.abs(o.x) / half.x, Math.abs(o.y) / half.y);
  return Math.min(FRAME_MAX, Math.max(FRAME_MIN, need * FRAME_MARGIN));
}
