import { communityColors, type ThemedColor } from "../lib/communityPalette";
import { visibleSubgraph, type Graph, type GraphFilters } from "./model";

export interface CommunitySummary {
  id: string;
  size: number;
  /** Имена участников по убыванию pagerank (не больше 3). */
  top: string[];
}

const score = (pagerank: number | null, mentions: number) => pagerank ?? mentions / 1e6;

/** Сообщества по убыванию размера; узлы без сообщества не входят. */
export function communitySummaries(g: Graph): CommunitySummary[] {
  const groups = new Map<string, { name: string; score: number }[]>();
  g.forEachNode((_, a) => {
    if (a.community === null) return;
    const list = groups.get(a.community) ?? [];
    list.push({ name: a.name, score: score(a.pagerank, a.mention_count) });
    groups.set(a.community, list);
  });
  return [...groups.entries()]
    .map(([id, list]) => ({
      id,
      size: list.length,
      top: list.sort((x, y) => y.score - x.score).slice(0, 3).map((x) => x.name),
    }))
    .sort((x, y) => y.size - x.size || x.id.localeCompare(y.id));
}

/**
 * Цвета всех сообществ графа: по порядку `communitySummaries` (крупнейшие — первыми),
 * поэтому фильтры цвет сообщества не меняют.
 */
export function communityColorMap(g: Graph): Map<string, ThemedColor> {
  return communityColors(communitySummaries(g).map((c) => c.id));
}

/** Имя сообщества для людей: два самых важных участника и сколько ещё. */
export function communityLabel(c: CommunitySummary): string {
  const named = c.top.slice(0, 2);
  const rest = c.size - named.length;
  return rest > 0 ? `${named.join(", ")} и ещё ${rest}` : named.join(", ");
}

/** Сколько сущностей каждого типа видно при текущих фильтрах, не считая фильтра типов. */
export function typeCounts(g: Graph, f: GraphFilters): Map<string, number> {
  const { nodes } = visibleSubgraph(g, { ...f, types: [] });
  const counts = new Map<string, number>();
  for (const id of nodes) {
    const type = g.getNodeAttribute(id, "type");
    counts.set(type, (counts.get(type) ?? 0) + 1);
  }
  return counts;
}

/**
 * Типы для чипов фильтра и легенды: настроенные типы, у которых есть узлы в текущем графе,
 * плюс выбранные (их можно снять), в порядке конфигурации. Без графа — только выбранные.
 */
export function shownTypes(order: readonly string[], g: Graph | null, selected: readonly string[]): string[] {
  const present = new Set<string>(selected);
  g?.forEachNode((_, a) => present.add(a.type));
  return order.filter((t) => present.has(t));
}

export interface PredicateCount {
  predicate: string;
  /** Связей с этим предикатом при текущих фильтрах (не считая фильтра связей). */
  count: number;
}

/**
 * Предикаты связей графа для фильтра: по убыванию числа связей во всём графе (порядок не
 * прыгает от фильтров), затем по имени; выбранные, которых в графе нет, — в конце с нулём.
 */
export function predicateCounts(g: Graph, f: GraphFilters): PredicateCount[] {
  const total = new Map<string, number>();
  g.forEachEdge((_, a) => total.set(a.predicate, (total.get(a.predicate) ?? 0) + 1));
  const visible = new Map<string, number>();
  for (const key of visibleSubgraph(g, { ...f, predicates: [] }).edges) {
    const p = g.getEdgeAttribute(key, "predicate");
    visible.set(p, (visible.get(p) ?? 0) + 1);
  }
  const rows = [...total.entries()]
    .sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
    .map(([predicate]) => ({ predicate, count: visible.get(predicate) ?? 0 }));
  for (const p of f.predicates) if (!total.has(p)) rows.push({ predicate: p, count: 0 });
  return rows;
}

/** Предикат для людей: `works_at` → «works at». */
export const predicateLabel = (p: string) => p.replaceAll("_", " ");

export interface RelationRow {
  key: string;
  direction: "out" | "in";
  predicate: string;
  otherId: string;
  otherName: string;
  weight: number;
}

/** Самые весомые связи сущности (входящие и исходящие). */
export function topRelations(g: Graph, id: string, limit = 8): RelationRow[] {
  if (!g.hasNode(id)) return [];
  const rows: RelationRow[] = [];
  g.forEachEdge(id, (key, a, src, dst) => {
    const out = src === id;
    const other = out ? dst : src;
    rows.push({
      key,
      direction: out ? "out" : "in",
      predicate: a.predicate,
      otherId: other,
      otherName: g.getNodeAttribute(other, "name"),
      weight: a.weight,
    });
  });
  return rows.sort((x, y) => y.weight - x.weight || x.key.localeCompare(y.key)).slice(0, limit);
}

/** Место каждой сущности по pagerank (1 — самая важная; без pagerank — по упоминаниям). */
export function rankByPagerank(g: Graph): Map<string, number> {
  const ids = g.mapNodes((id, a) => ({ id, s: score(a.pagerank, a.mention_count) }));
  ids.sort((x, y) => y.s - x.s);
  return new Map(ids.map((x, i) => [x.id, i + 1]));
}
