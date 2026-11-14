// Формы ответов HTTP API (`src/mnogobase/api/app.py`); фикстуры повторяют их же.
export interface NodeView {
  entity_id: string;
  name: string;
  type: string;
  mention_count: number;
  pagerank: number | null;
  community: string | null;
  degree: number;
}

export interface RelationView {
  src_id: string;
  src_name: string;
  predicate: string;
  dst_id: string;
  dst_name: string;
  description: string;
  weight: number;
  evidence: string[];
}

export interface GraphPayload {
  nodes: NodeView[];
  relations: RelationView[];
  ranked_by: "pagerank" | "mentions";
  truncated: boolean;
  /** Узлов, прошедших фильтры (в ответе — не больше `limit`). */
  total_nodes: number;
}

/** Ответ `GET /api/stats`. */
export interface Stats {
  documents: number;
  documents_by_status: Record<string, number>;
  chunks: number;
  entities: number;
  relations: number;
  wiki_pages: number;
  analysis: { ready: boolean; completed_at: string | null };
  /** Сначала загрузки в очереди (`updated_at: null`), затем изменённые последними. */
  recent: RecentDocument[];
}

/** Тип сущности: ключ из `extract.entity_types`, русская подпись и определение для показа. */
export interface EntityTypeInfo {
  type: string;
  label: string;
  description: string;
}

/** Ответ `GET /api/entities/types`: в порядке `extract.entity_types`. */
export interface TypeCount extends EntityTypeInfo {
  count: number;
}

export interface RecentDocument {
  id: string;
  name: string;
  status: string;
  updated_at: string | null;
}

/** Ответ `GET /api/auth/me`. */
export interface Me {
  id: string;
  username: string;
}

// ---- чат, документы, wiki, статус (поля — как в `service/kb.py` и `service/store.py`) ----

/** Источник ответа (`core.models.Source` + `KnowledgeBase._item`). */
export interface Source {
  /** Номер, на который ссылается текст ответа (`[n]`). */
  n: number;
  kind: "chunk" | "wiki" | "entity" | "relation";
  /** `chunk_id` фрагмента, `<page_id>#…` страницы wiki, `entity_id` сущности. */
  ref: string;
  /** Только имя файла (у загрузок — исходное имя), у wiki — `<slug>.md`. */
  path: string | null;
  page: number | null;
  /** Начало текста (до 200 символов). */
  snippet: string;
  /** Ответ на него сослался. */
  cited: boolean;
  /** У фрагмента документа. */
  doc_id?: string;
  /** Исходник документа (`/api/documents/<id>/content`) или страница wiki (`/api/wiki/<id>`). */
  url?: string;
  /** Только в истории диалога: фрагмент ещё в графе / файл страницы wiki есть. */
  available?: boolean;
}

/** Метрики ответа (`kb._meta`). */
export interface MessageMeta {
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  alt_queries: string[];
}

/** Сообщение диалога. */
export interface Message {
  id: string;
  conversation_id: string;
  user_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  mode: string;
  sources: Source[];
  status: "done" | "error";
  query: string | null;
  /** У старых сообщений и вопросов — `null`. */
  meta: MessageMeta | null;
  /** ISO 8601, UTC. */
  created_at: string;
}

export interface Conversation {
  id: string;
  user_id: string;
  title: string;
  /** ISO 8601, UTC. */
  created_at: string;
}

/** Задача очереди (`kb._job`): без пути и fingerprint. Метки времени — unix-секунды. */
export interface Job {
  id: number;
  kind: "ingest" | "remove" | "analyze";
  status: "pending" | "running" | "done" | "failed" | "superseded";
  attempts: number;
  available_at: number;
  /** Имя типа исключения (у `AnalysisError` — «Тип: текст»). */
  error: string | null;
  progress: string;
  created_at: number;
  updated_at: number;
  doc_id: string | null;
  name: string;
}

/** Ответ загрузки `POST /api/documents` (202). */
export interface UploadedJob extends Job {
  duplicate: boolean;
}

/** Строка списка документов. */
export interface DocumentListItem {
  id: string;
  name: string;
  /** Статус из registry или `queued` для загрузки, которую worker ещё не взял. */
  status: string;
  uploaded: boolean;
  url: string | null;
}

export interface DocumentStage {
  stage: string;
  status: string;
  attempts: number;
  error: string | null;
  updated_at: string | null;
}

export interface DocumentEntity {
  entity_id: string;
  name: string;
  type: string;
  has_page: boolean;
  slug: string | null;
}

/** Карточка документа `GET /api/documents/<id>`. */
export interface DocumentCard extends DocumentListItem {
  /** Все пять стадий по порядку; у загрузки в очереди — пусто. */
  stages: DocumentStage[];
  chunks: number;
  entities: DocumentEntity[];
}

/** Фрагмент `GET /api/chunks/<chunk_id>`. */
export interface ChunkView {
  chunk_id: string;
  doc_id: string;
  name: string;
  text: string;
  headings: string[];
  page: number | null;
  /** Только у загруженных через API документов. */
  url: string | null;
}

/** Строка списка wiki `GET /api/wiki`. */
export interface WikiListItem {
  page_id: string;
  slug: string;
  title: string;
  entity_type: string;
  aliases: string[];
}

/** Страница wiki `GET /api/wiki/<id>` и `/api/wiki/by-slug/<slug>`. */
export interface WikiPage {
  id: string;
  title: string;
  slug: string;
  /** Markdown с фронтматтером, сносками `[^chunk_id]` и разделом Sources. */
  content: string;
  /** `chunk_id` → имя документа; удалённых фрагментов здесь нет. */
  sources: Record<string, string>;
}

/** Строка расхода LLM. */
export interface UsageRow {
  model: string;
  currency: string | null;
  external: boolean;
  calls: number;
  tokens_in: number | null;
  tokens_out: number | null;
  errors: number;
  /** Строкой, чтобы не терять точность; `null` — тарифа нет. */
  estimated_cost: string | null;
}

/** Ответ `GET /api/status`. */
export interface StatusPayload {
  /** Число задач по статусам. */
  queue: Record<string, number>;
  running: Job[];
  worker: { last_seen: number | null; alive: boolean };
  llm_usage: { today: UsageRow[]; month: UsageRow[] };
}

/** Ответ `GET /api/graph/analysis`. */
export interface AnalysisPayload {
  revision: string | null;
  run_id: string | null;
  ready: boolean;
  completed_at: string | null;
  /** Отчёт `graph analyze`; пуст, если анализ не запускался. */
  metadata: { nodes?: number; relationships?: number; communities?: number; [key: string]: unknown };
  top: { entity_id: string; name: string; pagerank: number; community: string | null }[];
  job: Job | null;
}

/** Карточка сущности `GET /api/entities/<id>`. */
export interface EntityCard {
  entity_id: string;
  name: string;
  type: string;
  aliases: string[];
  description: string;
  descriptions: string[];
  mention_count: number;
  /** Из актуального анализа графа, иначе `null`. */
  pagerank: number | null;
  community: string | null;
  degree: number;
  documents_count: number;
  has_page: boolean;
  slug: string | null;
}

/** Строка каталога `GET /api/entities`: карточка узла + псевдонимы и страница wiki. */
export type EntityListItem = NodeView & { aliases: string[]; has_page: boolean; slug: string | null };

/** Ответ `GET /api/entities`: всего подходящих и страница. */
export interface EntityPage {
  total: number;
  items: EntityListItem[];
}

/** Сообщество актуального анализа: размер, до пяти главных узлов по PageRank, число по типам. */
export interface CommunityView {
  community: string;
  size: number;
  top: NodeView[];
  types: Record<string, number>;
}

/** Ответ `GET /api/graph/communities`; `ready: false` — анализ графа не запускался или устарел. */
export interface CommunitiesPayload {
  ready: boolean;
  total: number;
  items: CommunityView[];
}

/** Связь со страницы сущности: типы обоих концов для цвета точки. */
export type EntityRelation = RelationView & { src_type: string; dst_type: string };

/** Ответ `GET /api/entities/<id>/relations`: фасет `predicates` не учитывает фильтр по предикату. */
export interface EntityRelations {
  total: number;
  predicates: { predicate: string; count: number }[];
  items: EntityRelation[];
}

/** Фрагмент, упоминающий сущность (`GET /api/entities/<id>/mentions`). */
export interface EntityMention {
  chunk_id: string;
  doc_id: string;
  name: string;
  text: string;
  page: number | null;
  headings: string[];
  url: string | null;
}

/** Документ, упоминающий сущность (`GET /api/entities/<id>/documents`). */
export interface EntityDocument {
  doc_id: string;
  name: string;
  mentions: number;
  url: string | null;
}
