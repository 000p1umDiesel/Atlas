import { CHECK_LABELS, JOB_KIND_LABELS, KIND_LABELS, labelOf, MODES, STAGE_LABELS, STATUS_LABELS } from "./labels";

test("подписи из прежнего интерфейса", () => {
  expect(MODES.map((m) => [m.value, m.label, m.hint])).toEqual([
    ["rag", "Фрагменты", "поиск по тексту документов"],
    ["wiki", "Wiki", "по страницам wiki"],
    ["graph", "Граф", "по связям сущностей"],
    ["all", "Всё вместе", "все источники сразу"],
  ]);
  expect(STATUS_LABELS.superseded).toBe("заменена");
  expect(KIND_LABELS.relation).toBe("связь");
  expect(STAGE_LABELS.embed).toBe("Векторы для поиска");
  expect(JOB_KIND_LABELS.analyze).toBe("анализ графа");
  expect(CHECK_LABELS.graph_plugins).toBe("Плагины графа (APOC, GDS)");
});

test("неизвестное значение показывается как есть", () => {
  expect(labelOf(STATUS_LABELS, "done")).toBe("готово");
  expect(labelOf(STATUS_LABELS, "weird")).toBe("weird");
  // не наследуемые свойства объекта
  expect(labelOf(STATUS_LABELS, "constructor")).toBe("constructor");
});
