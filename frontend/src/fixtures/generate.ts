import type { GraphPayload, NodeView, RecentDocument, RelationView, Stats, TypeCount } from "../api/types";

/**
 * Типы сущностей — те же 18, что в `extract.entity_types` config.yaml, в его порядке
 * (`Other` последний).
 * Русская подпись и определение — форма будущего `GET /api/entities/types` (этап 2).
 */
const ENTITY_TYPES: { type: string; label: string; description: string }[] = [
  { type: "Person", label: "Человек", description: "Реальный или вымышленный человек, названный по имени или однозначно узнаваемый." },
  { type: "Organization", label: "Организация", description: "Компания, институт, университет, госорган, команда или сообщество — но не место, где они находятся." },
  { type: "Location", label: "Место", description: "Страна, город, регион, географический объект, здание или объект в космосе (планета, звезда, галактика)." },
  { type: "Event", label: "Событие", description: "Датированное событие — конференция, инцидент, битва, релиз, выборы — или исторический период." },
  { type: "Project", label: "Проект", description: "Именованная инициатива, программа или миссия с целью; не разовое событие." },
  { type: "Product", label: "Продукт", description: "Товар, услуга или бренд. Программы — в «ПО», приборы и машины — в «Оборудование»." },
  { type: "Equipment", label: "Оборудование", description: "Конкретный прибор, аппарат, машина или космический аппарат для работы и исследований (например, телескоп «Джеймс Уэбб»)." },
  { type: "Software", label: "ПО", description: "Программа, приложение, библиотека, фреймворк, язык программирования, платформа или обученная ML-модель (например, PyTorch, GPT-4)." },
  { type: "Technology", label: "Технология", description: "Общая технология, стандарт, протокол или класс устройств (например, 5G, блокчейн), а не конкретный продукт." },
  { type: "Method", label: "Метод", description: "Техника, алгоритм или процедура, которую применяют (например, градиентный спуск, ПЦР)." },
  { type: "Concept", label: "Понятие", description: "Абстрактная идея, теория, принцип, термин, природный процесс или целая дисциплина (например, инфляция, машинное обучение)." },
  { type: "Work", label: "Произведение", description: "Книга, статья, отчёт, журнал, фильм, датасет, бенчмарк или другая работа с названием." },
  { type: "Metric", label: "Метрика", description: "Мера, показатель или единица измерения (например, точность, BLEU, ВВП)." },
  { type: "Regulation", label: "Нормативный акт", description: "Закон, регламент, политика, договор или иной правовой акт." },
  { type: "Substance", label: "Вещество", description: "Химическое вещество, материал, лекарство, пищевой ингредиент или биомолекула (белок, гормон)." },
  { type: "Condition", label: "Состояние", description: "Болезнь, расстройство, симптом или иное медицинское либо психологическое состояние." },
  { type: "Organism", label: "Организм", description: "Вид, организм, тип клеток, орган или иное живое существо либо его часть." },
  { type: "Other", label: "Другое", description: "Всё значимое, что не подходит ни под один тип выше." },
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

// Число узлов по типам: в сумме 300 (как stats.entities). Понятий, людей, организаций,
// методов и произведений больше; нормативных актов, веществ, оборудования, состояний,
// организмов и «другого» — единицы.
const POOLS: Record<string, Pool> = {
  Person: {
    count: 35,
    bases: [
      "Иван Петров", "Анна Смирнова", "Дмитрий Волков", "Елена Кузнецова", "Алексей Морозов",
      "Ольга Новикова", "Сергей Соколов", "Мария Попова", "Andrew Ng", "Geoffrey Hinton",
      "Yann LeCun", "Ashish Vaswani", "Ilya Sutskever", "Fei-Fei Li", "Andrej Karpathy",
      "Николай Лебедев", "Татьяна Орлова", "Владимир Зайцев", "Yoshua Bengio", "Demis Hassabis",
    ],
    mods: ["мл.", "Jr.", "(автор)", "(редактор)"],
  },
  Organization: {
    count: 28,
    bases: [
      "Яндекс", "Сбер", "МГУ", "ВШЭ", "Сколтех", "Neo4j Inc.", "OpenAI", "Google Research",
      "Qdrant Solutions", "Hugging Face", "Anthropic", "Mail.ru Group", "ИТМО", "Институт системного анализа",
    ],
    mods: ["Labs", "Group", "(лаборатория ИИ)", "Europe"],
  },
  Location: {
    count: 16,
    bases: [
      "Москва", "Санкт-Петербург", "Новосибирск", "Mountain View", "Лондон", "Berlin",
      "Казань", "Сколково", "Cambridge", "Екатеринбург", "Марс", "Галактика Андромеды",
    ],
    mods: ["(кампус)", "Центр", "район"],
  },
  Event: {
    count: 14,
    bases: ["NeurIPS", "Конференция «Диалог»", "ACL", "ICML", "Хакатон ИИ", "AINL", "SIGIR", "Запуск ChatGPT"],
    mods: ["2022", "2023", "2024", "воркшоп"],
  },
  Project: {
    count: 12,
    bases: [
      "Проект «Многобаза»", "Human Genome Project", "Миссия Artemis", "BigScience", "Open Assistant",
      "Программа «Цифровая экономика»", "Проект ruGPT", "Horizon Europe",
    ],
    mods: ["фаза 2", "(пилот)", "2025"],
  },
  Product: {
    count: 10,
    bases: ["Яндекс Плюс", "iPhone 15", "Kindle Paperwhite", "Сбер Прайм", "ChatGPT Plus", "Яндекс Такси", "Pixel 8"],
    mods: ["Pro", "(подписка)", "Lite"],
  },
  Equipment: {
    count: 8,
    bases: [
      "Телескоп «Джеймс Уэбб»", "NVIDIA H100", "Большой адронный коллайдер", "Секвенатор NovaSeq",
      "Марсоход Curiosity", "Raspberry Pi 5",
    ],
    mods: ["(стенд)", "кластер"],
  },
  Software: {
    count: 23,
    bases: [
      "Qwen3-Embedding", "llama.cpp", "Docling", "RuBERT", "Neo4j", "Qdrant", "Ragas", "FastAPI",
      "PyTorch", "GPT-4", "Python", "Obsidian",
    ],
    mods: ["0.6B", "(форк)", "v3", "плагин"],
  },
  Technology: {
    count: 20,
    bases: [
      "5G", "Блокчейн", "CRISPR", "USB-C", "HTTP/3", "WebGPU", "Wi-Fi 7", "Литий-ионные аккумуляторы",
      "Векторные базы данных", "GPU-вычисления",
    ],
    mods: ["(стандарт)", "в России", "нового поколения"],
  },
  Method: {
    count: 28,
    bases: [
      "Leiden algorithm", "PageRank", "BM25", "Louvain method", "Reciprocal rank fusion", "Градиентный спуск",
      "Кросс-энкодер", "Dense retrieval", "Метод опорных векторов", "Chunking по смыслу", "Beam search", "LoRA",
      "ПЦР", "Дистилляция модели",
    ],
    mods: ["v2", "(вариант)", "с весами", "optimized", "для графов"],
  },
  Concept: {
    count: 41,
    bases: [
      "Трансформер", "Векторный поиск", "Граф знаний", "Внимание", "Эмбеддинг", "Retrieval-augmented generation",
      "Entity resolution", "Семантическая близость", "Токенизация", "Fine-tuning", "Контекстное окно",
      "Машинное обучение", "Инфляция", "Онтология", "Галлюцинация", "Zero-shot learning",
    ],
    mods: ["(обзор)", "в продакшене", "и качество", "for search", "в русском языке"],
  },
  Work: {
    count: 28,
    bases: [
      "Attention Is All You Need", "MS MARCO", "Natural Questions", "SQuAD", "RuBQ", "HotpotQA", "MTEB",
      "Книга «Глубокое обучение»", "Отчёт State of AI", "Журнал «Наука и жизнь»", "Обзор RAG", "Фильм «Она»",
    ],
    mods: ["(RU)", "2023", "test split", "второе издание"],
  },
  Metric: {
    count: 10,
    bases: ["Точность", "BLEU", "F1-мера", "ВВП", "nDCG@10", "Recall@k", "Перплексия", "MRR"],
    mods: ["(макро)", "на тесте"],
  },
  Regulation: {
    count: 7,
    bases: ["GDPR", "152-ФЗ «О персональных данных»", "EU AI Act", "Лицензия Apache 2.0", "Парижское соглашение"],
    mods: ["(редакция 2024)", "статья 6"],
  },
  Substance: {
    count: 6,
    bases: ["Кофеин", "Инсулин", "Графен", "Литий", "Гемоглобин"],
    mods: ["(образец)", "синтетический"],
  },
  Condition: {
    count: 5,
    bases: ["Диабет 2 типа", "Депрессия", "Мигрень", "COVID-19", "Бессонница", "Гипертония"],
    mods: ["(хроническая форма)"],
  },
  Organism: {
    count: 4,
    bases: ["Escherichia coli", "Дрозофила", "Нейрон", "Arabidopsis thaliana", "Homo sapiens"],
    mods: ["(линия)"],
  },
  Other: {
    count: 5,
    bases: ["Таблица 3", "Приложение Б", "Глоссарий проекта", "Слайд 12", "Сноска 4"],
    mods: ["(черновик)"],
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
} {
  const rnd = mulberry32(seed);

  // Узлы: тип по пулам, сообщество по кругу (гарантирует все 12), затем перемешивание.
  const raw: { name: string; type: string }[] = [];
  for (const { type } of ENTITY_TYPES) {
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
    // Как в графе: weight — сумма сил (целые 1..10 от модели) по подтверждающим фрагментам.
    // Число фрагментов геометрическое: обычно 1–2, редкие связи подтверждены многократно.
    let evidenceCount = 1;
    while (evidenceCount < 14 && rnd() < 0.42) evidenceCount++;
    let weight = 0;
    for (let i = 0; i < evidenceCount; i++) weight += 1 + Math.floor(rnd() ** 0.8 * 10);
    relations.push({
      src_id: src.entity_id,
      src_name: src.name,
      predicate,
      dst_id: dst.entity_id,
      dst_name: dst.name,
      description: `${src.name} — ${predicate.replace("_", " ")} — ${dst.name}`,
      weight,
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
  const types: TypeCount[] = ENTITY_TYPES.map((t) => ({ ...t, count: counts.get(t.type) ?? 0 }));

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
  };
}
