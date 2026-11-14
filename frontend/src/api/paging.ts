import { apiGet } from "./client";

/** Предел страницы списков сервера (documents, wiki). */
export const PAGE_SIZE = 200;

export type Paged<T> = { items: T[]; more: boolean };

/**
 * Первые `count` элементов списка страницами по 200; `more` — есть ли ещё: когда набрано ровно
 * `count`, пробуем `limit=1` со следующего места. `extra` (например, `q`) идёт после offset.
 */
export async function fetchPaged<T>(
  path: string,
  count: number,
  extra: Record<string, string | undefined> = {},
): Promise<Paged<T>> {
  const items: T[] = [];
  while (items.length < count) {
    const limit = Math.min(PAGE_SIZE, count - items.length);
    const batch = await apiGet<T[]>(path, { limit, offset: items.length, ...extra });
    items.push(...batch);
    if (batch.length < limit) return { items, more: false };
  }
  const probe = await apiGet<T[]>(path, { limit: 1, offset: items.length, ...extra });
  return { items, more: probe.length > 0 };
}
