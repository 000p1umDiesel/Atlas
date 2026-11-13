// Чистая часть 3D-графа: данные для three-forcegraph, подписи, кадрирование.
// Здесь нет three.js — модуль проверяется в jsdom без WebGL.
import { schemeColor, typeColorRgb } from "../lib/entityColors";
import { communityColorMap } from "./insights";
import { nodeSize, sizeMetric, sizeRange, type Graph, type GraphFilters } from "./model";

/** Узел для three-forcegraph; x/y/z/v* дописывает и двигает симуляция d3-force-3d. */
export interface ForceNode {
  id: string;
  name: string;
  /** Цвет `rgb(r, g, b)` (WebGL не понимает `var(...)`). */
  color: string;
  /** Радиус узла = `nodeSize` (4…22), та же шкала, что в 2D. */
  val: number;
  x?: number;
  y?: number;
  z?: number;
  vx?: number;
  vy?: number;
  vz?: number;
}

/** Связь; после загрузки в граф `source`/`target` заменяются объектами узлов. */
export interface ForceLink {
  source: string;
  target: string;
  weight: number;
}

export interface ForceData {
  nodes: ForceNode[];
  links: ForceLink[];
}

/** Порядок типов для цветов: настроенный или (без него) отсортированные типы всего графа. */
function typeOrder(graph: Graph, entityTypes?: string[]): string[] {
  if (entityTypes && entityTypes.length > 0) return entityTypes;
  return [...new Set(graph.mapNodes((_, a) => a.type))].sort();
}

/**
 * Видимая часть графа в формате three-forcegraph. Размер — через
 * `sizeMetric` → `sizeRange` → `nodeSize` (по всему графу, как в 2D), цвета — готовые
 * `rgb(...)`. Параллельные рёбра пары (в любую сторону) сливаются в одну связь
 * с наибольшим весом: в 3D они всё равно легли бы одной линией.
 */
export function toForceData(
  graph: Graph,
  visible: { nodes: Set<string>; edges: Set<string> },
  filters: Pick<GraphFilters, "colorBy" | "sizeBy">,
  entityTypes?: string[],
): ForceData {
  const metric = sizeMetric(graph, filters.sizeBy);
  const range = sizeRange(graph, filters.sizeBy);
  const types = typeOrder(graph, entityTypes);
  const communities = filters.colorBy === "community" ? communityColorMap(graph) : null;
  const nodes: ForceNode[] = [];
  graph.forEachNode((id, a) => {
    if (!visible.nodes.has(id)) return;
    const color = communities
      ? schemeColor(a.community === null ? undefined : communities.get(a.community))
      : typeColorRgb(a.type, types);
    nodes.push({ id, name: a.name, color, val: nodeSize(a, metric, range) });
  });
  const pairs = new Map<string, ForceLink>();
  graph.forEachEdge((key, a, src, dst) => {
    if (!visible.edges.has(key) || !visible.nodes.has(src) || !visible.nodes.has(dst) || src === dst) return;
    const pair = src < dst ? `${src}\u0000${dst}` : `${dst}\u0000${src}`;
    const weight = Number.isFinite(a.weight) ? a.weight : 0;
    const link = pairs.get(pair);
    if (link) link.weight = Math.max(link.weight, weight);
    else pairs.set(pair, { source: src, target: dst, weight });
  });
  return { nodes, links: [...pairs.values()] };
}

/**
 * Узлы с подписью: топ-`limit` по размеру (при равенстве — по id, детерминированно)
 * плюс обязательные из `always` (совпадения поиска, выбранный), если они видимы.
 */
export function labelCandidates(nodes: ForceNode[], limit: number, always: Iterable<string> = []): Set<string> {
  const top = [...nodes].sort((a, b) => b.val - a.val || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const out = new Set(top.slice(0, Math.max(0, limit)).map((n) => n.id));
  const ids = new Set(nodes.map((n) => n.id));
  for (const id of always) if (ids.has(id)) out.add(id);
  return out;
}

/** Совпадения поиска: подстрока имени без учёта регистра; пустая строка — ничего. */
export function highlightMatches(nodes: ForceNode[], highlight: string): Set<string> {
  const q = highlight.trim().toLowerCase();
  const out = new Set<string>();
  if (q === "") return out;
  for (const n of nodes) if (n.name.toLowerCase().includes(q)) out.add(n.id);
  return out;
}

export interface Point3 {
  x: number;
  y: number;
  z: number;
}

/**
 * Сфера, охватывающая все точки (центр — середина габаритного короба, радиус — до
 * самой дальней точки плюс её собственный радиус). Нечисловые координаты пропускаются.
 */
export function boundingSphere(points: Iterable<Point3 & { r: number }>): { center: Point3; radius: number } | null {
  const list = [...points].filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.z));
  if (list.length === 0) return null;
  const min = { x: Infinity, y: Infinity, z: Infinity };
  const max = { x: -Infinity, y: -Infinity, z: -Infinity };
  for (const p of list) {
    for (const k of ["x", "y", "z"] as const) {
      min[k] = Math.min(min[k], p[k]);
      max[k] = Math.max(max[k], p[k]);
    }
  }
  const center = { x: (min.x + max.x) / 2, y: (min.y + max.y) / 2, z: (min.z + max.z) / 2 };
  let radius = 1;
  for (const p of list) {
    const r = Number.isFinite(p.r) ? Math.max(0, p.r) : 0;
    radius = Math.max(radius, Math.hypot(p.x - center.x, p.y - center.y, p.z - center.z) + r);
  }
  return { center, radius };
}

/** Расстояние камеры, при котором сфера радиуса `radius` целиком видна (по узкой стороне). */
export function fitDistance(radius: number, verticalFovDeg: number, aspect: number): number {
  const vHalf = (verticalFovDeg / 2) * (Math.PI / 180);
  const hHalf = Math.atan(Math.tan(vHalf) * (aspect > 0 ? aspect : 1));
  return radius / Math.sin(Math.min(vHalf, hHalf));
}

/**
 * Узлы, оставшиеся после смены фильтров, сохраняют свой объект (и с ним позицию
 * и скорость в симуляции, и меш three-forcegraph); меняются только атрибуты.
 */
export function reconcileNodes(prev: Map<string, ForceNode>, next: ForceNode[]): ForceNode[] {
  return next.map((n) => {
    const old = prev.get(n.id);
    if (!old) return n;
    old.name = n.name;
    old.color = n.color;
    old.val = n.val;
    return old;
  });
}

/** Ключ состава (узлы и пары связей): цвет, размер и вес на него не влияют. */
export function compositionKey(data: { nodes: { id: string }[]; links: { source: string; target: string }[] }): string {
  const nodes = data.nodes.map((n) => n.id).sort();
  const links = data.links
    .map((l) => (l.source < l.target ? `${l.source}\u0000${l.target}` : `${l.target}\u0000${l.source}`))
    .sort();
  return `${nodes.join("\u0001")}\u0002${links.join("\u0001")}`;
}

const ALPHA_MIN = 0.2;
const ALPHA_MAX = 0.75;
const ALPHA_STEPS = 4;

/** Непрозрачность связи по весу: sqrt-шкала, 5 ступеней (меньше материалов в WebGL). */
export function linkAlpha(weight: number, maxWeight: number): number {
  if (!Number.isFinite(weight) || !(maxWeight > 0)) return ALPHA_MIN;
  const t = Math.sqrt(Math.min(1, Math.max(0, weight / maxWeight)));
  const step = Math.round(t * ALPHA_STEPS) / ALPHA_STEPS;
  return Math.round((ALPHA_MIN + step * (ALPHA_MAX - ALPHA_MIN)) * 1000) / 1000;
}

/** Плавный разгон и торможение (перелёт камеры). */
export function easeInOutCubic(t: number): number {
  if (!(t > 0)) return 0;
  if (t >= 1) return 1;
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}
