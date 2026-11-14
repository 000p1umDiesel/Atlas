// Документы и очередь обработки: списки (Query, с опросом) и изменения (`/api/documents`, `/api/jobs`).
import { keepPreviousData, useQuery, type QueryClient } from "@tanstack/react-query";
import { apiGet, apiSend } from "./client";
import type { DocumentListItem, Job } from "./types";

/** Страница списка документов (предел сервера) и шаг «Показать ещё». */
export const DOCUMENTS_PAGE = 200;
/** Последние задачи очереди. */
export const JOBS_LIMIT = 30;
/** Опрос, пока в очереди есть задачи `pending` или `running`. */
export const POLL_MS = 3000;

export const documentsKey = ["documents"] as const;
export const jobsKey = ["jobs"] as const;

export type DocumentPage = { items: DocumentListItem[]; more: boolean };

/**
 * Первые `count` документов страницами по 200; `more` — есть ли ещё: когда набрано ровно `count`,
 * пробуем `limit=1` со следующего места.
 */
export async function fetchDocuments(count: number): Promise<DocumentPage> {
  const items: DocumentListItem[] = [];
  while (items.length < count) {
    const limit = Math.min(DOCUMENTS_PAGE, count - items.length);
    const batch = await apiGet<DocumentListItem[]>("/api/documents", { limit, offset: items.length });
    items.push(...batch);
    if (batch.length < limit) return { items, more: false };
  }
  const probe = await apiGet<DocumentListItem[]>("/api/documents", { limit: 1, offset: items.length });
  return { items, more: probe.length > 0 };
}

/** В очереди есть работа: тогда списки опрашиваются. */
export function hasActiveJobs(jobs: readonly Job[] | undefined): boolean {
  return (jobs ?? []).some((job) => job.status === "pending" || job.status === "running");
}

/**
 * Первые `count` документов. `poll` — опрашивать раз в 3000 мс (страница включает его, пока в
 * очереди есть работа). При «Показать ещё» прежний список виден, пока грузится длинный.
 */
export function useDocuments(count: number, { poll = false }: { poll?: boolean } = {}) {
  return useQuery<DocumentPage>({
    queryKey: [...documentsKey, count],
    queryFn: () => fetchDocuments(count),
    placeholderData: keepPreviousData,
    // статусы меняет worker: при каждом открытии страницы — свежий список
    staleTime: 0,
    refetchInterval: poll ? POLL_MS : false,
  });
}

/** Последние 30 задач; опрос раз в 3000 мс, пока среди них есть `pending|running`. */
export function useJobs() {
  return useQuery<Job[]>({
    queryKey: jobsKey,
    queryFn: () => apiGet<Job[]>("/api/jobs", { limit: JOBS_LIMIT }),
    staleTime: 0,
    refetchInterval: (query) => (hasActiveJobs(query.state.data) ? POLL_MS : false),
  });
}

/** Перечитать документы и очередь (после загрузки, удаления, повтора). */
export function refreshDocuments(client: QueryClient): Promise<void> {
  return Promise.all([
    client.invalidateQueries({ queryKey: documentsKey }),
    client.invalidateQueries({ queryKey: jobsKey }),
  ]).then(() => undefined);
}

/** Удаление из базы — задача `remove` в очереди (сам файл worker забудет позже). */
export async function deleteDocument(id: string): Promise<Job> {
  return (await apiSend<Job>("DELETE", `/api/documents/${encodeURIComponent(id)}`))!;
}

/** Стадия обработки документа (`stages[]` карточки). */
export interface DocumentStage {
  stage: string;
  status: string;
  attempts: number;
  error: string | null;
  updated_at: string | null;
}

/** Сущность документа в карточке. */
export interface DocumentEntity {
  entity_id: string;
  name: string;
  type: string;
  has_page: boolean;
  slug: string | null;
}

/** Ответ `GET /api/documents/<id>`. */
export interface DocumentDetail {
  id: string;
  name: string;
  status: string;
  uploaded: boolean;
  url: string | null;
  stages: DocumentStage[];
  chunks: number;
  entities: DocumentEntity[];
}

export interface ReaderChunk {
  chunk_id: string;
  idx: number;
  text: string;
  headings: string[];
  page_start: number | null;
  page_end: number | null;
}

/** Ответ `GET /api/documents/<id>/chunks`. */
export interface ChunkPage {
  document: { id: string; name: string; title: string | null; n_pages: number | null };
  total: number;
  chunks: ReaderChunk[];
}

/** Фрагментов на странице чтения (предел по умолчанию для сервера — 50). */
export const READER_PAGE = 50;

/** Карточка документа: без опроса, статусы читаются при каждом открытии. */
export function useDocument(id: string) {
  return useQuery<DocumentDetail>({
    queryKey: ["document", id],
    queryFn: () => apiGet<DocumentDetail>(`/api/documents/${encodeURIComponent(id)}`),
    staleTime: 0,
  });
}

/** Страница фрагментов документа для чтения; при смене страницы прежняя не показывается. */
export function useDocumentChunks(id: string, offset: number) {
  return useQuery<ChunkPage>({
    queryKey: ["document-chunks", id, offset],
    queryFn: () =>
      apiGet<ChunkPage>(`/api/documents/${encodeURIComponent(id)}/chunks`, { limit: READER_PAGE, offset }),
    staleTime: 0,
  });
}

/** Повтор упавшей задачи. */
export async function retryJob(id: number): Promise<void> {
  await apiSend("POST", `/api/jobs/${encodeURIComponent(String(id))}/retry`);
}
