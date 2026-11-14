import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { createTypeCatalog, type TypeCatalog } from "../lib/typeCatalog";
import { ApiError, apiGet } from "./client";
import type { GraphPayload, RecentDocument, Stats, TypeCount } from "./types";

/** Узлов в обзоре графа: главная и страница графа читают один и тот же запрос. */
const OVERVIEW_LIMIT = 500;

// Фикстуры подгружаются динамически и попадают в сборку только при VITE_FIXTURES=1: проверка
// флага стоит прямо перед import(), иначе сборщик не выбросит модуль фикстур из настоящей сборки.
// Флаг читается при вызове (Vite всё равно подставляет его при сборке), чтобы тесты могли его менять.
export function fixturesMode(): boolean {
  return import.meta.env.VITE_FIXTURES === "1";
}

async function fixtures() {
  if (import.meta.env.VITE_FIXTURES !== "1") throw new Error("Фикстуры выключены");
  const { generateFixtures } = await import("../fixtures/generate");
  return generateFixtures();
}

/** Повтор запроса (один раз): не повторяем, если ответ не изменится — нет входа, нет объекта, неверный запрос. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && [401, 404, 422].includes(error.status)) return false;
  return failureCount < 1;
}

async function fetchStats(): Promise<Stats> {
  return fixturesMode() ? (await fixtures()).stats : apiGet<Stats>("/api/stats");
}

export function useStats() {
  return useQuery<Stats>({ queryKey: ["stats"], queryFn: fetchStats });
}

export function useOverview() {
  return useQuery<{ graph: GraphPayload }>({
    queryKey: ["overview", OVERVIEW_LIMIT],
    queryFn: async () => ({
      graph: fixturesMode()
        ? (await fixtures()).graph
        : await apiGet<GraphPayload>("/api/graph/overview", { limit: OVERVIEW_LIMIT }),
    }),
  });
}

/** Типы сущностей в порядке `extract.entity_types`: ключ, русская подпись, определение, число. */
export function useEntityTypes() {
  return useQuery<TypeCount[]>({
    queryKey: ["entity-types"],
    queryFn: async () => (fixturesMode() ? (await fixtures()).types : apiGet<TypeCount[]>("/api/entities/types")),
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

/** Последние документы — часть сводки: тот же запрос `/api/stats`, без второго обращения. */
export function useRecentDocuments() {
  return useQuery<Stats, Error, RecentDocument[]>({
    queryKey: ["stats"],
    queryFn: fetchStats,
    select: (stats) => stats.recent,
  });
}
