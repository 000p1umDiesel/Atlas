import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useMemo } from "react";

export type EntitySort = "relevance" | "mentions" | "pagerank" | "degree" | "name";
export type EntityOrder = "asc" | "desc";

/**
 * Поиск и фильтры каталога `/entities`. `sort`/`order` = `null` — по умолчанию сервера:
 * `relevance` при поиске, иначе `mentions`; `asc` для `name`, иначе `desc`.
 */
export interface EntitiesSearch {
  /** Как набрано (обрезка — при запросе): эхо адреса не трогает пробелы в поле. */
  q: string;
  types: string[];
  community: string | null;
  sort: EntitySort | null;
  order: EntityOrder | null;
  minMentions: number;
}

const SORTS: readonly EntitySort[] = ["relevance", "mentions", "pagerank", "degree", "name"];

/** Строка из URL (парсер может отдать число). */
function str(v: unknown): string | undefined {
  if (typeof v === "string") return v;
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return undefined;
}

/** Список: повтор ключа или через запятую; пустые и повторы отбрасываются. */
function list(v: unknown): string[] {
  const parts = Array.isArray(v) ? v.flatMap((x) => (typeof x === "string" ? x.split(",") : [])) : (str(v)?.split(",") ?? []);
  return [...new Set(parts.map((x) => x.trim()).filter(Boolean))];
}

function count(v: unknown): number {
  const raw = str(v);
  const n = raw !== undefined && raw.trim() !== "" ? Number(raw) : NaN;
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

/** Разбор search-параметров `/entities`; неверное — `null` или значение по умолчанию. */
export function parseEntitiesSearch(raw: Record<string, unknown>): EntitiesSearch {
  const q = str(raw.q) ?? "";
  const sort = SORTS.includes(raw.sort as EntitySort) ? (raw.sort as EntitySort) : null;
  const order = raw.order === "asc" || raw.order === "desc" ? raw.order : null;
  return {
    q,
    types: list(raw.type),
    community: str(raw.community)?.trim() || null,
    // Без поиска релевантности нет: сервер всё равно отсортирует по упоминаниям.
    sort: sort === "relevance" && !q.trim() ? null : sort,
    order,
    minMentions: count(raw.minMentions),
  };
}

/** Обратно в URL: значения по умолчанию не пишутся, типы — через запятую в `type`. */
export function serializeEntitiesSearch(s: EntitiesSearch): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  if (s.q.trim()) out.q = s.q;
  if (s.types.length > 0) out.type = s.types.join(",");
  if (s.community !== null) out.community = s.community;
  if (s.sort !== null) out.sort = s.sort;
  if (s.order !== null) out.order = s.order;
  if (s.minMentions > 0) out.minMentions = s.minMentions;
  return out;
}

const defaultSort = (q: string): EntitySort => (q.trim() ? "relevance" : "mentions");
const defaultOrder = (sort: EntitySort): EntityOrder => (sort === "name" ? "asc" : "desc");

/** Сортировка, по которой сервер упорядочит ответ. */
export function effectiveSort(s: EntitiesSearch): EntitySort {
  return s.sort ?? defaultSort(s.q);
}

export function effectiveOrder(s: EntitiesSearch): EntityOrder {
  return s.order ?? defaultOrder(effectiveSort(s));
}

/** Новая сортировка: порядок — снова по умолчанию; сортировка по умолчанию не пишется. */
export function withSort(s: EntitiesSearch, sort: EntitySort): EntitiesSearch {
  return { ...s, sort: sort === defaultSort(s.q) ? null : sort, order: null };
}

export function withOrder(s: EntitiesSearch, order: EntityOrder): EntitiesSearch {
  return { ...s, order: order === defaultOrder(effectiveSort(s)) ? null : order };
}

/** Есть ли что ослаблять: поиск или фильтры (сортировка — не фильтр). */
export function hasFilters(s: EntitiesSearch): boolean {
  return s.q.trim() !== "" || s.types.length > 0 || s.community !== null || s.minMentions > 0;
}

/** search-параметры `/entities` для маршрута: разобранные и чистые, `panel` сохраняется. */
export function validateEntitiesSearch(raw: Record<string, unknown>): Record<string, string | number> {
  const out = serializeEntitiesSearch(parseEntitiesSearch(raw));
  return typeof raw.panel === "string" ? { ...out, panel: raw.panel } : out;
}

type Update = (next: (s: EntitiesSearch) => EntitiesSearch, opts?: { replace?: boolean }) => void;

/** Состояние каталога в URL: запись — от свежего адреса, через `validateEntitiesSearch`. */
export function useEntitiesSearch(): { search: EntitiesSearch; update: Update } {
  const raw = useSearch({ strict: false }) as Record<string, unknown>;
  const search = useMemo(() => parseEntitiesSearch(raw), [raw]);
  const navigate = useNavigate();
  const update = useCallback<Update>(
    (next, opts) => {
      void navigate({
        to: "/entities",
        search: ((prev: Record<string, unknown>) => {
          const out = serializeEntitiesSearch(next(parseEntitiesSearch(prev)));
          return typeof prev.panel === "string" ? { ...out, panel: prev.panel } : out;
        }) as never,
        replace: opts?.replace,
      });
    },
    [navigate],
  );
  return { search, update };
}
