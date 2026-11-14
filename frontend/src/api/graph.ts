// Части графа вокруг сущности (окрестность), пути и сообщества, которые не входят в обзор.
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./client";
import type { CommunityGraph, GraphPart, PathPayload } from "./types";

/** Участников сообщества на графе: самые важные по PageRank. */
export const COMMUNITY_LIMIT = 300;

/** Окрестность сущности глубиной 1–3, не больше `limit` узлов; `id === null` — без запроса. */
export function useNeighbors(id: string | null, depth: 1 | 2 | 3, limit: number) {
  return useQuery<GraphPart>({
    queryKey: ["neighbors", id, depth, limit],
    queryFn: () => apiGet<GraphPart>(`/api/graph/${encodeURIComponent(id!)}/neighbors`, { depth, limit }),
    enabled: id !== null,
  });
}

/** Кратчайших путей в ответе не больше. */
export const PATHS_LIMIT = 5;

/** Кратчайшие пути длиной до `hops` (1–5); без любого из концов или при `source === target` — без запроса. */
export function usePath(source: string | null, target: string | null, hops: number) {
  return useQuery<PathPayload>({
    queryKey: ["path", source, target, hops],
    queryFn: () =>
      apiGet<PathPayload>("/api/graph/path", { source: source!, target: target!, max_hops: hops, limit: PATHS_LIMIT }),
    enabled: source !== null && target !== null && source !== target,
  });
}

/** Подграф сообщества актуального анализа; `community === null` — без запроса. */
export function useCommunity(community: string | null) {
  return useQuery<CommunityGraph>({
    queryKey: ["community", community, COMMUNITY_LIMIT],
    queryFn: () =>
      apiGet<CommunityGraph>(`/api/graph/communities/${encodeURIComponent(community!)}`, { limit: COMMUNITY_LIMIT }),
    enabled: community !== null,
  });
}
