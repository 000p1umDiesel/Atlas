// Части графа вокруг сущности (окрестность), которые не входят в обзор.
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./client";
import type { GraphPart } from "./types";

/** Окрестность сущности глубиной 1–3, не больше `limit` узлов; `id === null` — без запроса. */
export function useNeighbors(id: string | null, depth: 1 | 2 | 3, limit: number) {
  return useQuery<GraphPart>({
    queryKey: ["neighbors", id, depth, limit],
    queryFn: () => apiGet<GraphPart>(`/api/graph/${encodeURIComponent(id!)}/neighbors`, { depth, limit }),
    enabled: id !== null,
  });
}
