import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  applyTheme,
  getThemeChoice,
  loadThemeChoice,
  resolveTheme,
  saveThemeChoice,
  setThemeChoice,
  useTheme,
} from "./theme";

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

test("setThemeChoice уведомляет всех: общий выбор для любых useTheme", () => {
  setThemeChoice("light");
  const seen: string[] = [];
  const views = [0, 1].map(() => renderHook(() => useTheme()));
  act(() => views[0]!.result.current[1]("dark"));
  for (const v of views) seen.push(v.result.current[0]);
  expect(seen).toEqual(["dark", "dark"]);
  expect(getThemeChoice()).toBe("dark");
  expect(localStorage.getItem("mb.theme")).toBe("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
});
