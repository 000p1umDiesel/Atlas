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
