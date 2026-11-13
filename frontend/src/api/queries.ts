import { useQuery } from "@tanstack/react-query";
import type { GraphPayload, RecentDocument, Stats, TypeCount } from "./types";

const USE_FIXTURES = import.meta.env.VITE_FIXTURES === "1";

// Фикстуры подгружаются динамически и попадают в сборку только при VITE_FIXTURES=1.
async function fixtures() {
  if (!USE_FIXTURES) throw new Error("API появится на этапе 2");
  const { generateFixtures } = await import("../fixtures/generate");
  return generateFixtures();
}

export function useStats() {
  return useQuery<Stats>({ queryKey: ["stats"], queryFn: async () => (await fixtures()).stats });
}

export function useOverview() {
  return useQuery<{ graph: GraphPayload; types: TypeCount[] }>({
    queryKey: ["overview"],
    queryFn: async () => {
      const f = await fixtures();
      return { graph: f.graph, types: f.types };
    },
  });
}

export function useEntityTypes() {
  return useQuery<string[]>({
    queryKey: ["entity-types"],
    queryFn: async () => (await fixtures()).entityTypes,
  });
}

export function useRecentDocuments() {
  return useQuery<RecentDocument[]>({
    queryKey: ["recent-documents"],
    queryFn: async () => (await fixtures()).recent,
  });
}
