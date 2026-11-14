// Данные графа текущего режима: обзор, окрестность, путь между двумя сущностями, сообщество.
import { useMemo } from "react";
import { useCommunity, useNeighbors, usePath } from "../api/graph";
import { fixturesMode, useOverview } from "../api/queries";
import type { GraphPart, PathView } from "../api/types";
import { mergeParts } from "./dataset";
import { buildGraph, type Graph } from "./model";
import type { GraphMode, GraphSearch } from "./search";

/** Узлов в окрестности сущности (ответ сервера ограничен этим числом). */
export const NEIGHBORS_LIMIT = 200;

export type GraphData =
  | { state: "input"; prompt: string }
  | { state: "pending" }
  | { state: "error"; error: unknown }
  | {
      state: "ready";
      graph: Graph;
      truncated: boolean;
      /** Режим «Сообщество»: настоящий размер сообщества (на графе — не больше 300 участников). */
      communitySize?: number;
      /** Режим «Путь»: найденные пути (граф — их объединение) и признак таймаута поиска. */
      paths?: PathView[];
      timedOut?: boolean;
    };

export const PROMPTS = {
  neighbors: "Выберите сущность, чтобы увидеть её окрестность.",
  community: "Выберите сообщество.",
  path: "Выберите две сущности, чтобы найти путь между ними.",
  samePath: "Выберите две разные сущности.",
} as const;

/** Режим, который реально показывается: на фикстурах есть только обзор. */
export function activeMode(search: Pick<GraphSearch, "mode">): GraphMode {
  return fixturesMode() ? "overview" : search.mode;
}

type Source = {
  /** Основная часть графа режима (без раскрытий). */
  part: GraphPart | undefined;
  truncated: boolean;
  communitySize?: number;
  paths?: PathView[];
  timedOut?: boolean;
  error: unknown;
  isError: boolean;
};

/**
 * Граф текущего режима. Запросы общие через кэш Query: страница и колонка фильтров зовут
 * хук независимо, сеть — одна. Граф собирается из основной части (и раскрытий — потом)
 * и пересобирается только при смене данных.
 */
export function useGraphData(search: GraphSearch): GraphData {
  const mode = activeMode(search);
  const overview = useOverview(mode === "overview");
  const neighbors = useNeighbors(mode === "neighbors" ? search.center : null, search.depth, NEIGHBORS_LIMIT);
  const community = useCommunity(mode === "community" ? search.community : null);
  const path = usePath(mode === "path" ? search.source : null, mode === "path" ? search.target : null, search.hops);
  // Пути — одна часть графа; пересобирается только с новым ответом.
  const pathPart = useMemo(() => (path.data ? mergeParts(path.data.paths) : undefined), [path.data]);

  let source: Source | null = null;
  let prompt: string | null = null;
  if (mode === "overview") {
    source = {
      part: overview.data?.graph,
      truncated: overview.data?.graph.truncated ?? false,
      error: overview.error,
      isError: overview.isError,
    };
  } else if (mode === "neighbors") {
    if (search.center === null) prompt = PROMPTS.neighbors;
    // У окрестности нет признака усечения: ответ и так ограничен `limit`.
    else source = { part: neighbors.data, truncated: false, error: neighbors.error, isError: neighbors.isError };
  } else if (mode === "community") {
    if (search.community === null) prompt = PROMPTS.community;
    else
      source = {
        part: community.data,
        truncated: community.data?.truncated ?? false,
        communitySize: community.data?.size,
        error: community.error,
        isError: community.isError,
      };
  } else if (search.source === null || search.target === null) {
    prompt = PROMPTS.path;
  } else if (search.source === search.target) {
    prompt = PROMPTS.samePath;
  } else {
    source = {
      part: pathPart,
      truncated: false,
      paths: path.data?.paths,
      timedOut: path.data?.timed_out,
      error: path.error,
      isError: path.isError,
    };
  }

  const part = source?.part;
  const graph = useMemo(() => (part ? buildGraph(mergeParts([part])) : null), [part]);

  if (prompt !== null) return { state: "input", prompt };
  if (source?.isError) return { state: "error", error: source.error };
  if (!graph || !source) return { state: "pending" };
  const ready: GraphData = { state: "ready", graph, truncated: source.truncated };
  if (source.communitySize !== undefined) ready.communitySize = source.communitySize;
  if (source.paths !== undefined) {
    ready.paths = source.paths;
    ready.timedOut = source.timedOut ?? false;
  }
  return ready;
}
