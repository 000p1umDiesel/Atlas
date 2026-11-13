import { expect, test } from "vitest";
import { cssColor, typeColorVar } from "./entityColors";

test("typeColorVar берёт цвет по индексу типа", () => {
  expect(typeColorVar("Person", ["Person", "Org"])).toBe("var(--t1)");
  expect(typeColorVar("Org", ["Person", "Org"])).toBe("var(--t2)");
});

test("typeColorVar циклически повторяет 8 цветов", () => {
  const types = Array.from({ length: 9 }, (_, i) => `T${i}`);
  expect(typeColorVar("T8", types)).toBe("var(--t1)");
});

test("неизвестный тип получает нейтральный цвет", () => {
  expect(typeColorVar("Nope", ["Person"])).toBe("var(--t0)");
});

test("cssColor разворачивает вложенные var()", () => {
  document.documentElement.style.setProperty("--a", "var(--b)");
  document.documentElement.style.setProperty("--b", "#d8a48f");
  expect(cssColor("--a")).toBe("#d8a48f");
  expect(cssColor("var(--a)")).toBe("#d8a48f");
});
