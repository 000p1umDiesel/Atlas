import { stringifyPlainSearch } from "../lib/searchParams";
import { DEFAULT_FILTERS, type ColorBy, type GraphFilters, type SizeBy } from "./model";

export type GraphView = "2d" | "3d";
/** Режим графа: обзор всего графа, окрестность сущности, путь между двумя, сообщество. */
export type GraphMode = "overview" | "neighbors" | "path" | "community";
export type Depth = 1 | 2 | 3;

/** Параметры режима: центр окрестности, концы пути, раскрытые узлы. */
export type ModeState = {
  mode: GraphMode;
  center: string | null;
  depth: Depth;
  source: string | null;
  target: string | null;
  /** Наибольшая длина пути (1–5). */
  hops: number;
  /** Узлы, соседи которых добавлены к графу (последние 20). */
  expand: string[];
};

/** Фильтры, вид, режим и выбранный узел (`focus` — entity_id; null — ничего не выбрано). */
export type GraphSearch = GraphFilters & ModeState & { view: GraphView; focus: string | null };
/** Для ссылок на граф: режим и выбранный узел можно не указывать. */
export type GraphSearchInput = GraphFilters & Partial<ModeState> & { view: GraphView; focus?: string | null };

/** Узлов в `expand` не больше: старые вытесняются. */
export const EXPAND_LIMIT = 20;
const DEFAULT_DEPTH: Depth = 1;
const DEFAULT_HOPS = 4;
const MAX_HOPS = 5;

export const DEFAULT_MODE: ModeState = {
  mode: "overview",
  center: null,
  depth: DEFAULT_DEPTH,
  source: null,
  target: null,
  hops: DEFAULT_HOPS,
  expand: [],
};

export const DEFAULT_GRAPH_SEARCH: GraphSearch = { ...DEFAULT_FILTERS, ...DEFAULT_MODE, view: "2d", focus: null };

/** id сущности: hex из ядра (`core/ids.py`) или id фикстур; всё прочее — не фокус. */
const ENTITY_ID = /^[\w.:-]{1,128}$/;

const D = DEFAULT_FILTERS;

/** Строка из URL. Парсер TanStack превращает «числовые» строки в числа (`highlight=123`). */
function str(v: unknown): string | undefined {
  if (typeof v === "string") return v;
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return undefined;
}

function num(v: unknown, fallback: number): number {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

/** Целое из диапазона [min, max], иначе — дефолт. */
function intIn(v: unknown, min: number, max: number, fallback: number): number {
  const n = num(v, NaN);
  return Number.isInteger(n) && n >= min && n <= max ? n : fallback;
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.includes(v as T) ? (v as T) : fallback;
}

function list(v: unknown): string[] {
  const parts = Array.isArray(v) ? v : typeof v === "string" ? v.split(",") : [];
  return parts.filter((x): x is string => typeof x === "string").map((x) => x.trim()).filter(Boolean);
}

function entityId(v: unknown): string | null {
  const s = str(v)?.trim();
  return s && ENTITY_ID.test(s) ? s : null;
}

/** Раскрытые узлы: корректные id без повторов, не больше последних `EXPAND_LIMIT`. */
function expandList(v: unknown): string[] {
  const ids = [...new Set(list(v).filter((id) => ENTITY_ID.test(id)))];
  return ids.slice(-EXPAND_LIMIT);
}

/** Разбор search-параметров /graph; неверные значения заменяются дефолтами. */
export function parseGraphSearch(raw: Record<string, unknown>): GraphSearch {
  return {
    types: list(raw.types),
    community: str(raw.community) || null,
    minWeight: num(raw.minWeight, D.minWeight),
    minMentions: num(raw.minMentions, D.minMentions),
    hideIsolated: raw.hideIsolated === true || raw.hideIsolated === "true",
    colorBy: oneOf<ColorBy>(raw.colorBy, ["type", "community"], D.colorBy),
    sizeBy: oneOf<SizeBy>(raw.sizeBy, ["pagerank", "mentions", "degree"], D.sizeBy),
    highlight: str(raw.highlight) ?? D.highlight,
    predicates: [...new Set(list(raw.predicates))],
    mode: oneOf<GraphMode>(raw.mode, ["overview", "neighbors", "path", "community"], DEFAULT_MODE.mode),
    center: entityId(raw.center),
    depth: intIn(raw.depth, 1, 3, DEFAULT_DEPTH) as Depth,
    source: entityId(raw.source),
    target: entityId(raw.target),
    hops: intIn(raw.hops, 1, MAX_HOPS, DEFAULT_HOPS),
    expand: expandList(raw.expand),
    view: oneOf<GraphView>(raw.view, ["2d", "3d"], "2d"),
    focus: entityId(raw.focus),
  };
}

/**
 * Обратное преобразование: значения по умолчанию опускаются (чистые URL). Порядок — как
 * в `graphLink`: сначала режим и его параметры, затем фильтры, вид и выбранный узел.
 */
export function serializeGraphSearch(s: GraphSearchInput): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  if (s.mode && s.mode !== DEFAULT_MODE.mode) out.mode = s.mode;
  if (s.center) out.center = s.center;
  if (s.depth !== undefined && s.depth !== DEFAULT_DEPTH) out.depth = s.depth;
  if (s.source) out.source = s.source;
  if (s.target) out.target = s.target;
  if (s.hops !== undefined && s.hops !== DEFAULT_HOPS) out.hops = s.hops;
  if (s.expand && s.expand.length > 0) out.expand = s.expand.join(",");
  if (s.types.length > 0) out.types = s.types.join(",");
  if (s.community !== null) out.community = s.community;
  if (s.predicates.length > 0) out.predicates = s.predicates.join(",");
  if (s.minWeight !== D.minWeight) out.minWeight = s.minWeight;
  if (s.minMentions !== D.minMentions) out.minMentions = s.minMentions;
  if (s.hideIsolated !== D.hideIsolated) out.hideIsolated = s.hideIsolated;
  if (s.colorBy !== D.colorBy) out.colorBy = s.colorBy;
  if (s.sizeBy !== D.sizeBy) out.sizeBy = s.sizeBy;
  if (s.highlight !== D.highlight) out.highlight = s.highlight;
  if (s.view !== "2d") out.view = s.view;
  if (s.focus) out.focus = s.focus;
  return out;
}

/** Что показать на графе по ссылке: окрестность сущности, путь от неё или сообщество. */
export type GraphLinkTarget =
  | { mode: "neighbors"; center: string }
  | { mode: "path"; source: string; target?: string }
  | { mode: "community"; community: string };

/** Адрес графа в нужном режиме; у окрестности центр сразу выбран (`focus`). */
export function graphLink(p: GraphLinkTarget): string {
  const search =
    p.mode === "neighbors"
      ? { mode: p.mode, center: p.center, focus: p.center }
      : p.mode === "path"
        ? { mode: p.mode, source: p.source, target: p.target }
        : { mode: p.mode, community: p.community };
  return `/graph${stringifyPlainSearch(search)}`;
}
