import type { GraphPayload, NodeView, RecentDocument, RelationView, Stats, TypeCount } from "../api/types";

const ENTITY_TYPES = [
  "Person",
  "Organization",
  "Concept",
  "Method",
  "Dataset",
  "Place",
  "Event",
  "Artifact",
];
const PREDICATES = [
  "uses",
  "part_of",
  "created_by",
  "related_to",
  "located_in",
  "cites",
  "improves",
  "evaluated_on",
];
const COMMUNITY_COUNT = 12;
const RELATION_TARGET = 700;

/** Детерминированный PRNG (mulberry32). */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Pool {
  count: number;
  bases: string[];
  mods: string[];
}

const POOLS: Record<string, Pool> = {
  Person: {
    count: 50,
    bases: [
      "Иван Петров", "Анна Смирнова", "Дмитрий Волков", "Елена Кузнецова", "Алексей Морозов",
      "Ольга Новикова", "Сергей Соколов", "Мария Попова", "Andrew Ng", "Geoffrey Hinton",
      "Yann LeCun", "Ashish Vaswani", "Ilya Sutskever", "Fei-Fei Li", "Andrej Karpathy",
      "Николай Лебедев", "Татьяна Орлова", "Владимир Зайцев", "Yoshua Bengio", "Demis Hassabis",
    ],
    mods: ["мл.", "Jr.", "(автор)", "(редактор)"],
  },
  Organization: {
    count: 36,
    bases: [
      "Яндекс", "Сбер", "МГУ", "ВШЭ", "Сколтех", "Neo4j Inc.", "OpenAI", "Google Research",
      "Qdrant Solutions", "Hugging Face", "Anthropic", "Mail.ru Group", "ИТМО", "Институт системного анализа",
    ],
    mods: ["Labs", "Group", "(лаборатория ИИ)", "Europe"],
  },
  Concept: {
    count: 56,
    bases: [
      "Трансформер", "Векторный поиск", "Граф знаний", "Внимание", "Эмбеддинг", "Retrieval-augmented generation",
      "Entity resolution", "Семантическая близость", "Токенизация", "Fine-tuning", "Контекстное окно",
      "Knowledge graph", "Attention mechanism", "Онтология", "Галлюцинация", "Zero-shot learning",
    ],
    mods: ["(обзор)", "в продакшене", "и качество", "for search", "в русском языке"],
  },
  Method: {
    count: 50,
    bases: [
      "Leiden algorithm", "PageRank", "BM25", "Louvain method", "Reciprocal rank fusion", "Градиентный спуск",
      "Кросс-энкодер", "Dense retrieval", "Метод опорных векторов", "Chunking по смыслу", "Beam search", "LoRA",
      "Кластеризация", "Дистилляция модели",
    ],
    mods: ["v2", "(вариант)", "с весами", "optimized", "для графов"],
  },
  Dataset: {
    count: 30,
    bases: [
      "MS MARCO", "Natural Questions", "Корпус РуСЕИТ", "SQuAD", "RuBQ", "Wikipedia dump",
      "Taiga corpus", "HotpotQA", "Open Corpus РИА",
    ],
    mods: ["(RU)", "2023", "test split", "очищенный", "mini"],
  },
  Place: {
    count: 26,
    bases: [
      "Москва", "Санкт-Петербург", "Новосибирск", "Mountain View", "Лондон", "Berlin",
      "Казань", "Сколково", "Cambridge", "Екатеринбург",
    ],
    mods: ["(кампус)", "Центр", "Downtown", "район"],
  },
  Event: {
    count: 22,
    bases: [
      "NeurIPS", "Dialogue конференция", "ACL", "ICML", "Хакатон ИИ", "AINL", "RuSSIR", "SIGIR",
    ],
    mods: ["2022", "2023", "2024", "воркшоп"],
  },
  Artifact: {
    count: 30,
    bases: [
      "Qwen3-Embedding", "llama.cpp", "Docling", "RuBERT", "Neo4j", "Qdrant", "Ragas", "FastAPI",
      "Obsidian vault", "Модель GPT",
    ],
    mods: ["0.6B", "(форк)", "v3", "плагин", "в Docker"],
  },
};

const CHUNK_TOPICS = ["abstract", "intro", "method", "results", "обзор", "заключение"];

function shuffle<T>(arr: T[], rnd: () => number): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [a[i], a[j]] = [a[j]!, a[i]!];
  }
  return a;
}

function makeNames(pool: Pool, rnd: () => number): string[] {
  const names: string[] = [...pool.bases];
  const used = new Set(names);
  const all: string[] = [];
  for (const b of pool.bases) for (const m of pool.mods) all.push(`${b} ${m}`);
  for (const n of shuffle(all, rnd)) {
    if (names.length >= pool.count) break;
    if (!used.has(n)) {
      used.add(n);
      names.push(n);
    }
  }
  return shuffle(names.slice(0, pool.count), rnd);
}

function pick<T>(arr: T[], rnd: () => number): T {
  return arr[Math.floor(rnd() * arr.length)]!;
}

export function generateFixtures(seed = 7): {
  graph: GraphPayload;
  stats: Stats;
  types: TypeCount[];
  recent: RecentDocument[];
  entityTypes: string[];
} {
  const rnd = mulberry32(seed);

  // Узлы: тип по пулам, сообщество по кругу (гарантирует все 12), затем перемешивание.
  const raw: { name: string; type: string }[] = [];
  for (const type of ENTITY_TYPES) {
    for (const name of makeNames(POOLS[type]!, rnd)) raw.push({ name, type });
  }
  const order = shuffle(raw, rnd);

  // PageRank степенной: ранги перемешаны независимо от порядка узлов.
  const ranks = shuffle(
    order.map((_, i) => i + 1),
    rnd,
  );
  const prRaw = ranks.map((r) => Math.pow(r, -0.9));
  const prSum = prRaw.reduce((s, x) => s + x, 0);

  const nodes: NodeView[] = order.map((o, i) => {
    const rank = ranks[i]!;
    return {
      entity_id: `ent-${seed}-${String(i).padStart(3, "0")}`,
      name: o.name,
      type: o.type,
      mention_count: Math.max(1, Math.round(120 * Math.pow(rank, -0.8) + rnd() * 3)),
      pagerank: prRaw[i]! / prSum,
      community: `c${(i % COMMUNITY_COUNT) + 1}`,
      degree: 0,
    };
  });

  const byCommunity = new Map<string, NodeView[]>();
  for (const n of nodes) {
    const list = byCommunity.get(n.community!) ?? [];
    list.push(n);
    byCommunity.set(n.community!, list);
  }
  // Притяжение к «важным» узлам: выбор из пары по большему pagerank.
  const preferential = (list: NodeView[]) => {
    const a = pick(list, rnd);
    const b = pick(list, rnd);
    return a.pagerank! >= b.pagerank! ? a : b;
  };

  const relations: RelationView[] = [];
  const seen = new Set<string>();
  let guard = 0;
  while (relations.length < RELATION_TARGET && guard++ < RELATION_TARGET * 20) {
    const src = rnd() < 0.5 ? pick(nodes, rnd) : preferential(nodes);
    const dst =
      rnd() < 0.8 ? preferential(byCommunity.get(src.community!)!) : preferential(nodes);
    if (src.entity_id === dst.entity_id) continue;
    const predicate = pick(PREDICATES, rnd);
    const key = `${src.entity_id}|${predicate}|${dst.entity_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const evidenceCount = 1 + Math.floor(rnd() * 3);
    relations.push({
      src_id: src.entity_id,
      src_name: src.name,
      predicate,
      dst_id: dst.entity_id,
      dst_name: dst.name,
      description: `${src.name} — ${predicate.replace("_", " ")} — ${dst.name}`,
      weight: Math.round((0.1 + rnd() * 0.9) * 100) / 100,
      evidence: Array.from(
        { length: evidenceCount },
        () =>
          `doc${Math.floor(rnd() * 40).toString(16).padStart(4, "0")}:${String(Math.floor(rnd() * 30)).padStart(5, "0")}`,
      ),
    });
    src.degree++;
    dst.degree++;
  }

  const counts = new Map<string, number>();
  for (const n of nodes) counts.set(n.type, (counts.get(n.type) ?? 0) + 1);
  const types: TypeCount[] = ENTITY_TYPES.map((type) => ({ type, count: counts.get(type) ?? 0 }));

  const statuses = ["done", "done", "done", "running", "pending", "failed"];
  const exts = ["pdf", "docx", "md", "pptx"];
  const docBases = ["Отчёт по графам знаний", "RAG survey", "Протокол встречи", "Neo4j handbook", "Лекция по NLP", "Embedding benchmark"];
  const recent: RecentDocument[] = Array.from({ length: 8 }, (_, i) => ({
    id: `doc-${seed}-${i}`,
    name: `${pick(docBases, rnd)} ${CHUNK_TOPICS[i % CHUNK_TOPICS.length]}.${pick(exts, rnd)}`,
    status: i === 0 ? "running" : pick(statuses, rnd),
    uploaded: new Date(Date.UTC(2026, 10, 13 - i, 12, Math.floor(rnd() * 60))).toISOString(),
  }));

  const stats: Stats = {
    documents: 40,
    chunks: 1260,
    entities: nodes.length,
    relations: relations.length,
    wiki_pages: 187,
    analysis: { ready: true, completed_at: "2026-11-12T21:40:00.000Z" },
  };

  return {
    graph: { nodes, relations, ranked_by: "pagerank", truncated: false },
    stats,
    types,
    recent,
    entityTypes: [...ENTITY_TYPES],
  };
}
