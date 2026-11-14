// Быстрый поиск палитры Ctrl+K: сущности, wiki, документы и свои диалоги одним запросом.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { apiGet } from "./client";
import type { QuickSearch } from "./types";

/**
 * `GET /api/search/quick?q=` по обрезанному тексту; пустой текст запроса не шлёт. Ответ на
 * устаревший текст ложится под свой ключ и новый не перетирает; пока идёт новый запрос,
 * виден прежний ответ.
 */
export function useQuickSearch(q: string) {
  const text = q.trim();
  return useQuery<QuickSearch>({
    queryKey: ["quick", text],
    queryFn: () => apiGet<QuickSearch>("/api/search/quick", { q: text }),
    enabled: text !== "",
    placeholderData: keepPreviousData,
  });
}
