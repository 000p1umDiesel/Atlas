// Русские подписи значений API (статусы, режимы, виды источников, стадии, задачи, проверки) —
// дословно из прежнего интерфейса. Неизвестное значение показывается как есть (`labelOf`).

/** Статус документа или задачи. */
export const STATUS_LABELS: Readonly<Record<string, string>> = {
  queued: "в очереди",
  pending: "ждёт",
  running: "в работе",
  processing: "обрабатывается",
  done: "готово",
  failed: "ошибка",
  superseded: "заменена",
};

export type Mode = "rag" | "wiki" | "graph" | "all";

/** Режимы поиска ответа в порядке показа: подпись и подсказка. */
export const MODES: readonly { value: Mode; label: string; hint: string }[] = [
  { value: "rag", label: "Фрагменты", hint: "поиск по тексту документов" },
  { value: "wiki", label: "Wiki", hint: "по страницам wiki" },
  { value: "graph", label: "Граф", hint: "по связям сущностей" },
  { value: "all", label: "Всё вместе", hint: "все источники сразу" },
];

/** Вид источника ответа. */
export const KIND_LABELS: Readonly<Record<string, string>> = {
  chunk: "документ",
  wiki: "wiki",
  entity: "сущность",
  relation: "связь",
};

/** Стадия обработки документа (в порядке конвейера). */
export const STAGE_LABELS: Readonly<Record<string, string>> = {
  parse: "Разбор файла",
  chunk: "Нарезка на фрагменты",
  embed: "Векторы для поиска",
  extract: "Извлечение сущностей",
  graph: "Запись в граф",
};

/** Вид задачи очереди. */
export const JOB_KIND_LABELS: Readonly<Record<string, string>> = {
  ingest: "загрузка",
  remove: "удаление",
  analyze: "анализ графа",
};

/** Проверка сервиса (`/api/status/checks`). */
export const CHECK_LABELS: Readonly<Record<string, string>> = {
  database: "PostgreSQL",
  models: "Серверы моделей (эмбеддинги, реранкер, зрение)",
  llm: "LLM",
  qdrant: "Qdrant (векторы)",
  neo4j: "Neo4j (граф)",
  graph_plugins: "Плагины графа (APOC, GDS)",
};

/** Подпись значения; неизвестное (в том числе имя свойства объекта) — само значение. */
export function labelOf(table: Readonly<Record<string, string>>, value: string): string {
  return Object.hasOwn(table, value) ? table[value]! : value;
}
