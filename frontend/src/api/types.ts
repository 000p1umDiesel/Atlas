// Формы будущего API (этап 2); пока данные приходят из фикстур.
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
}

export interface Stats {
  documents: number;
  chunks: number;
  entities: number;
  relations: number;
  wiki_pages: number;
  analysis: { ready: boolean; completed_at: string | null };
}

export interface TypeCount {
  type: string;
  count: number;
}

export interface RecentDocument {
  id: string;
  name: string;
  status: string;
  uploaded: string;
}
