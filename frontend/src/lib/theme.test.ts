import { afterEach, expect, test, vi } from "vitest";
import { applyTheme, loadThemeChoice, resolveTheme, saveThemeChoice } from "./theme";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

test("resolveTheme учитывает системную тему только для system", () => {
  expect(resolveTheme("system", true)).toBe("dark");
  expect(resolveTheme("system", false)).toBe("light");
  expect(resolveTheme("light", true)).toBe("light");
  expect(resolveTheme("dark", false)).toBe("dark");
});

test("loadThemeChoice переживает бросающее хранилище", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(loadThemeChoice()).toBe("system");
});

test("loadThemeChoice игнорирует мусор", () => {
  localStorage.setItem("mb.theme", "purple");
  expect(loadThemeChoice()).toBe("system");
});

test("saveThemeChoice и loadThemeChoice работают парой", () => {
  saveThemeChoice("dark");
  expect(loadThemeChoice()).toBe("dark");
});

test("saveThemeChoice не падает при бросающем хранилище", () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(() => saveThemeChoice("light")).not.toThrow();
});

test("applyTheme ставит и снимает data-theme", () => {
  applyTheme("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
  applyTheme("system");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});
