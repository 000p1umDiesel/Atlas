import { DEFAULT_FILTERS, type ColorBy, type GraphFilters, type SizeBy } from "./model";

export type GraphView = "2d" | "3d";
/** Фильтры, вид и выбранный узел (`focus` — entity_id; null — ничего не выбрано). */
export type GraphSearch = GraphFilters & { view: GraphView; focus: string | null };
/** Для ссылок на граф: выбранный узел можно не указывать. */
export type GraphSearchInput = GraphFilters & { view: GraphView; focus?: string | null };

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

function oneOf<T extends string>(v: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.includes(v as T) ? (v as T) : fallback;
}

function list(v: unknown): string[] {
  const parts = Array.isArray(v) ? v : typeof v === "string" ? v.split(",") : [];
  return parts.filter((x): x is string => typeof x === "string").map((x) => x.trim()).filter(Boolean);
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
    view: oneOf<GraphView>(raw.view, ["2d", "3d"], "2d"),
    focus: entityId(raw.focus),
  };
}

function entityId(v: unknown): string | null {
  const s = str(v)?.trim();
  return s && ENTITY_ID.test(s) ? s : null;
}

/** Обратное преобразование: значения по умолчанию опускаются (чистые URL). */
export function serializeGraphSearch(s: GraphSearchInput): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  if (s.types.length > 0) out.types = s.types.join(",");
  if (s.community !== null) out.community = s.community;
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
