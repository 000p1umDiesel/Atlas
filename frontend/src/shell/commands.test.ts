import { ACTIONS, filterActions, nextTheme, type PaletteAction } from "./commands";

const labels = (actions: readonly PaletteAction[]) => actions.map((a) => a.label);

test("пустой запрос — все действия по порядку", () => {
  expect(filterActions("")).toEqual(ACTIONS);
  expect(filterActions("   ")).toEqual(ACTIONS);
  expect(labels(ACTIONS)).toEqual([
    "Новый чат",
    "Загрузить документ",
    "Сравнить сущности",
    "Перейти: Главная",
    "Перейти: Чат",
    "Перейти: Документы",
    "Перейти: Сущности",
    "Перейти: Граф",
    "Перейти: Wiki",
    "Перейти: Статус",
    "Сменить тему",
  ]);
});

test("«тем» находит «Сменить тему»", () => {
  expect(labels(filterActions("тем"))).toEqual(["Сменить тему"]);
});

test("регистр не важен: «ГРАФ» → «Перейти: Граф»", () => {
  expect(labels(filterActions("ГРАФ"))).toEqual(["Перейти: Граф"]);
});

test("ё и е не различаются ни в запросе, ни в ключевых словах", () => {
  const hedgehog: PaletteAction = { id: "x", label: "Колючий", keywords: "ёжик", run: "navigate", to: "/" };
  expect(labels(filterActions("ежик", [hedgehog]))).toEqual(["Колючий"]);
  expect(labels(filterActions("ЁЖИК", [hedgehog]))).toEqual(["Колючий"]);
  expect(labels(filterActions("тёму"))).toEqual(["Сменить тему"]);
});

test("несколько слов — все должны найтись", () => {
  expect(labels(filterActions("перейти wiki"))).toEqual(["Перейти: Wiki"]);
  expect(filterActions("перейти ёжик")).toEqual([]);
});

test("переходы ведут на разделы, тема — по кругу system → light → dark", () => {
  const to = Object.fromEntries(ACTIONS.map((a) => [a.label, a.to]));
  expect(to["Новый чат"]).toBe("/chat");
  expect(to["Загрузить документ"]).toBe("/documents");
  expect(to["Перейти: Главная"]).toBe("/");
  expect(to["Перейти: Сущности"]).toBe("/entities");
  expect(ACTIONS.find((a) => a.label === "Сменить тему")?.run).toBe("theme");
  expect(nextTheme("system")).toBe("light");
  expect(nextTheme("light")).toBe("dark");
  expect(nextTheme("dark")).toBe("system");
});
