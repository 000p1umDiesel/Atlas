// Части графа вокруг сущности (окрестность) и сообщества, которые не входят в обзор.
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./client";
import type { CommunityGraph, GraphPart } from "./types";

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

/** Подграф сообщества актуального анализа; `community === null` — без запроса. */
export function useCommunity(community: string | null) {
  return useQuery<CommunityGraph>({
    queryKey: ["community", community, COMMUNITY_LIMIT],
    queryFn: () =>
      apiGet<CommunityGraph>(`/api/graph/communities/${encodeURIComponent(community!)}`, { limit: COMMUNITY_LIMIT }),
    enabled: community !== null,
  });
}
