// Статус: очередь и worker, расход LLM, анализ графа, проверки сервисов.
import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { apiGet, apiSend } from "./client";
import type { AnalysisPayload, Job, StatusPayload } from "./types";

/** Опрос статуса и (пока задача активна) анализа. */
export const STATUS_POLL_MS = 5000;

export const statusKey = ["status"] as const;
export const analysisKey = ["analysis"] as const;

/** Состояние обработки; опрашивается всегда (в фоновой вкладке Query не опрашивает). */
export function useStatus() {
  return useQuery<StatusPayload>({
    queryKey: statusKey,
    queryFn: () => apiGet<StatusPayload>("/api/status"),
    staleTime: 0,
    refetchInterval: STATUS_POLL_MS,
  });
}

/** Задача анализа ждёт или идёт. */
export function analysisActive(data: AnalysisPayload | undefined): boolean {
  return data?.job?.status === "pending" || data?.job?.status === "running";
}

/** Снимок анализа; опрос раз в 5000 мс только пока задача активна. */
export function useAnalysis() {
  const client = useQueryClient();
  const query = useQuery<AnalysisPayload>({
    queryKey: analysisKey,
    queryFn: () => apiGet<AnalysisPayload>("/api/graph/analysis"),
    staleTime: 0,
    refetchInterval: (q) => (analysisActive(q.state.data) ? STATUS_POLL_MS : false),
  });
  // Задача закончилась (done/failed): сводка и граф главной (PageRank, сообщества), список
  // сообществ и статус читают результат из своих запросов.
  const wasActive = useRef(false);
  const active = analysisActive(query.data);
  useEffect(() => {
    if (wasActive.current && !active) {
      void client.invalidateQueries({ queryKey: statusKey });
      void client.invalidateQueries({ queryKey: ["stats"] });
      void client.invalidateQueries({ queryKey: ["overview"] });
      void client.invalidateQueries({ queryKey: ["communities"] });
      void client.invalidateQueries({ queryKey: ["community"] });
    }
    wasActive.current = active;
  }, [active, client]);
  return query;
}

/** Перечитать всё, что зависит от анализа: снимок, статус очереди, сводку, граф и сообщества. */
export function refreshAnalysis(client: QueryClient): Promise<void> {
  return Promise.all([
    client.invalidateQueries({ queryKey: analysisKey }),
    client.invalidateQueries({ queryKey: statusKey }),
    client.invalidateQueries({ queryKey: ["stats"] }),
    client.invalidateQueries({ queryKey: ["overview"] }),
    client.invalidateQueries({ queryKey: ["communities"] }),
    client.invalidateQueries({ queryKey: ["community"] }),
  ]).then(() => undefined);
}

/** Запуск анализа графа: `POST /api/graph/analyze` (202, задача в очереди). */
export function useStartAnalysis() {
  const client = useQueryClient();
  return useMutation<Job | null, Error>({
    mutationFn: () => apiSend<Job>("POST", "/api/graph/analyze"),
    onSuccess: () => refreshAnalysis(client),
  });
}

export interface ServiceCheck {
  name: string;
  ok: boolean;
  detail: string;
  warn: boolean;
}

/** Быстрые проверки сервисов — только по кнопке, без кэша. */
export function runChecks(): Promise<ServiceCheck[]> {
  return apiGet<ServiceCheck[]>("/api/status/checks");
}
