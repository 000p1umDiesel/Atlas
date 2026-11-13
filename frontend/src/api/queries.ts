import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { createTypeCatalog, type TypeCatalog } from "../lib/typeCatalog";
import type { GraphPayload, RecentDocument, Stats, TypeCount } from "./types";

// Фикстуры подгружаются динамически и попадают в сборку только при VITE_FIXTURES=1.
// Флаг читается при вызове (Vite всё равно подставляет его при сборке), чтобы тесты могли его менять.
async function fixtures() {
  if (import.meta.env.VITE_FIXTURES !== "1") throw new Error("API появится на этапе 2");
  const { generateFixtures } = await import("../fixtures/generate");
  return generateFixtures();
}

export function useStats() {
  return useQuery<Stats>({ queryKey: ["stats"], queryFn: async () => (await fixtures()).stats });
}

export function useOverview() {
  return useQuery<{ graph: GraphPayload }>({
    queryKey: ["overview"],
    queryFn: async () => ({ graph: (await fixtures()).graph }),
  });
}

/** Типы сущностей в порядке `extract.entity_types`: ключ, русская подпись, определение, число. */
export function useEntityTypes() {
  return useQuery<TypeCount[]>({
    queryKey: ["entity-types"],
    queryFn: async () => (await fixtures()).types,
  });
}

/**
 * Справочник типов из того же запроса: подписи, определения и цвета для всех компонентов.
 * Пока список не загружен — ключи вместо подписей и порядок пуст.
 */
export function useTypeCatalog(): TypeCatalog {
  const { data } = useEntityTypes();
  return useMemo(() => createTypeCatalog(data), [data]);
}

export function useRecentDocuments() {
  return useQuery<RecentDocument[]>({
    queryKey: ["recent-documents"],
    queryFn: async () => (await fixtures()).recent,
  });
}
