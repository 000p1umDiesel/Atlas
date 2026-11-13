import { createTypeCatalog } from "./typeCatalog";
import { typeColorAt } from "./typePalette";

const types = [
  { type: "Person", label: "Человек", description: "Реальный человек.", count: 3 },
  { type: "Organization", label: "Организация", description: "Компания.", count: 1 },
];

test("подпись и определение берутся из списка типов", () => {
  const c = createTypeCatalog(types);
  expect(c.order).toEqual(["Person", "Organization"]);
  expect(c.typeLabel("Organization")).toBe("Организация");
  expect(c.typeInfo("Person")).toEqual({ type: "Person", label: "Человек", description: "Реальный человек.", known: true });
});

test("неизвестный тип: подпись — сам ключ, определения нет, цвет нейтральный", () => {
  const c = createTypeCatalog(types);
  expect(c.typeLabel("Dataset")).toBe("Dataset");
  expect(c.typeInfo("Dataset")).toEqual({ type: "Dataset", label: "Dataset", description: "", known: false });
  expect(c.typeColor("Dataset")).toBe("var(--t0)");
});

test("пока список не загружен — ключи вместо подписей", () => {
  const c = createTypeCatalog(undefined);
  expect(c.order).toEqual([]);
  expect(c.typeLabel("Person")).toBe("Person");
});

test("пустая подпись из API → ключ", () => {
  const c = createTypeCatalog([{ type: "Work", label: " ", description: "", count: 0 }]);
  expect(c.typeLabel("Work")).toBe("Work");
});

test("цвет типа — по позиции в списке, light-dark() для DOM", () => {
  const c = createTypeCatalog(types);
  const second = typeColorAt(1);
  expect(c.typeColor("Organization")).toBe(`light-dark(${second.light}, ${second.dark})`);
});

test("«Другое» (Other) — с подписью и определением, но нейтрального цвета; следующий тип не сдвигается", () => {
  const c = createTypeCatalog([
    { type: "Person", label: "Человек", description: "Человек.", count: 1 },
    { type: "Other", label: "Другое", description: "Всё прочее.", count: 1 },
    { type: "Work", label: "Произведение", description: "Книга.", count: 1 },
  ]);
  expect(c.typeLabel("Other")).toBe("Другое");
  expect(c.typeInfo("Other").known).toBe(true);
  expect(c.typeColor("Other")).toBe("var(--t0)");
  const second = typeColorAt(1);
  expect(c.typeColor("Work")).toBe(`light-dark(${second.light}, ${second.dark})`);
});
