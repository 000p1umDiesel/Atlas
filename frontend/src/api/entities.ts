// Каталог сущностей и сообщества актуального анализа графа.
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { EntitiesSearch } from "../entities/search";
import { apiGet, type QueryParams } from "./client";
import type { CommunitiesPayload, CommunityView, EntityPage } from "./types";

/** Строк каталога за один запрос. */
export const ENTITY_PAGE = 50;

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
    getNextPageParam: (last, _pages, offset) => {
      const next = offset + last.items.length;
      return last.items.length > 0 && next < last.total ? next : undefined;
    },
    placeholderData: keepPreviousData,
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
