// Wiki: список страниц (с поиском) и сама страница по slug или id.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { apiGet } from "./client";
import { fetchPaged, PAGE_SIZE, type Paged } from "./paging";
import type { WikiListItem, WikiPage } from "./types";

export const WIKI_PAGE = PAGE_SIZE;

/** Первые `count` страниц; `q` — подстрока названия, slug или псевдонима (на сервере). */
export function useWikiList(q: string, count: number) {
  return useQuery<Paged<WikiListItem>>({
    queryKey: ["wiki", q, count],
    queryFn: () => fetchPaged<WikiListItem>("/api/wiki", count, q ? { q } : {}),
    placeholderData: keepPreviousData,
    staleTime: 0,
  });
}

/** Страница по slug (`/wiki/<slug>`) или по id (`/wiki/page/<id>`). 404 — удалена или устарела. */
export function useWikiPage(ref: { slug: string } | { pageId: string }) {
  const path =
    "slug" in ref
      ? `/api/wiki/by-slug/${encodeURIComponent(ref.slug)}`
      : `/api/wiki/${encodeURIComponent(ref.pageId)}`;
  return useQuery<WikiPage>({ queryKey: ["wiki-page", path], queryFn: () => apiGet<WikiPage>(path), staleTime: 0 });
}
