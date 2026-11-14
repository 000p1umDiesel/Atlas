// Каталог сущностей и сообщества актуального анализа графа.
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { EntitiesSearch } from "../entities/search";
import { apiGet, type QueryParams } from "./client";
import type {
  CommunitiesPayload,
  CommunityView,
  EntityCard,
  EntityDocument,
  EntityMention,
  EntityPage,
  EntityRelations,
} from "./types";

/** Строк каталога за один запрос. */
export const ENTITY_PAGE = 50;
/** Связей сущности за один запрос. */
export const RELATIONS_PAGE = 50;
/** Упоминаний сущности за один запрос. */
export const MENTIONS_PAGE = 20;
/** Документов сущности — одним списком. */
const DOCUMENTS_LIMIT = 200;

export type RelationDirection = "both" | "out" | "in";

/** Следующий `offset`, пока `offset + items < total` (пустая страница — конец). */
function nextOffset(last: { total: number; items: unknown[] }, offset: number): number | undefined {
  const next = offset + last.items.length;
  return last.items.length > 0 && next < last.total ? next : undefined;
}

const entityPath = (id: string, tail = "") => `/api/entities/${encodeURIComponent(id)}${tail}`;

/** Параметры `GET /api/entities` в порядке сервера; пустой поиск и нулевой порог не шлются. */
function entityParams(s: EntitiesSearch, offset: number): QueryParams {
  const q = s.q.trim();
  return {
    q: q || undefined,
    type: s.types,
    community: s.community ?? undefined,
    min_mentions: s.minMentions > 0 ? s.minMentions : undefined,
    sort: s.sort ?? undefined,
    order: s.order ?? undefined,
    limit: ENTITY_PAGE,
    offset,
  };
}

/**
 * Каталог страницами по 50: следующая есть, пока `offset + items < total`. При смене фильтров
 * остаётся прежний список (без мигания); ответ на устаревший ввод ложится в свой ключ и не виден.
 */
export function useEntityList(s: EntitiesSearch) {
  return useInfiniteQuery({
    queryKey: ["entities", s],
    queryFn: ({ pageParam }) => apiGet<EntityPage>("/api/entities", entityParams(s, pageParam)),
    initialPageParam: 0,
    getNextPageParam: (last, _pages, offset) => nextOffset(last, offset),
    placeholderData: keepPreviousData,
  });
}

/** Карточка сущности; тот же ключ, что у правой панели, — данные общие. `null` — без запроса. */
export function useEntity(id: string | null) {
  return useQuery<EntityCard>({
    queryKey: ["entity", id],
    queryFn: () => apiGet<EntityCard>(entityPath(id!)),
    enabled: id !== null,
  });
}

/**
 * Связи сущности страницами по 50 (параметры в порядке сервера). При смене направления или
 * предиката остаётся прежний список, пока не придёт новый.
 */
export function useEntityRelations(id: string, direction: RelationDirection, predicate: string | null) {
  return useInfiniteQuery({
    queryKey: ["entity-relations", id, direction, predicate],
    queryFn: ({ pageParam }) =>
      apiGet<EntityRelations>(entityPath(id, "/relations"), {
        direction,
        predicate: predicate ?? undefined,
        limit: RELATIONS_PAGE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (last, _pages, offset) => nextOffset(last, offset),
    placeholderData: keepPreviousData,
  });
}

/** Фрагменты, упоминающие сущность, страницами по 20. */
export function useEntityMentions(id: string) {
  return useInfiniteQuery({
    queryKey: ["entity-mentions", id],
    queryFn: ({ pageParam }) =>
      apiGet<{ total: number; items: EntityMention[] }>(entityPath(id, "/mentions"), {
        limit: MENTIONS_PAGE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (last, _pages, offset) => nextOffset(last, offset),
  });
}

/** Документы, упоминающие сущность (до 200, по числу упоминаний). */
export function useEntityDocuments(id: string) {
  return useQuery<EntityDocument[]>({
    queryKey: ["entity-documents", id],
    queryFn: () => apiGet<EntityDocument[]>(entityPath(id, "/documents"), { limit: DOCUMENTS_LIMIT }),
  });
}

/** Самые крупные сообщества актуального анализа (`ready: false` — анализа нет). */
export function useCommunities(limit: number) {
  return useQuery<CommunitiesPayload>({
    queryKey: ["communities", limit],
    queryFn: () => apiGet<CommunitiesPayload>("/api/graph/communities", { limit }),
  });
}

/** Название сообщества — имена до трёх главных участников. */
export function communityTitle(c: CommunityView): string {
  const names = c.top.slice(0, 3).map((n) => n.name);
  return names.length > 0 ? names.join(", ") : `Сообщество ${c.community}`;
}
