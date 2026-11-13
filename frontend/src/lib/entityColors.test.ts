import { afterEach, expect, test, vi } from "vitest";
import {
  clearCssColorCache,
  cssColor,
  normalizeColor,
  schemeColor,
  typeColorVar,
  withAlpha,
} from "./entityColors";

/** Поддельный 2D-контекст: jsdom не умеет canvas. Отдаёт заданный пиксель. */
function fakeContext(pixel: [number, number, number, number]) {
  const ctx = {
    fillStyle: "#000000",
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(pixel) })),
  };
  return ctx;
}

afterEach(() => {
  vi.restoreAllMocks();
  clearCssColorCache();
  document.documentElement.removeAttribute("style");
});

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

test("cssColor разворачивает вложенные var() и отдаёт rgb()", () => {
  document.documentElement.style.setProperty("--a", "var(--b)");
  document.documentElement.style.setProperty("--b", "#d8a48f");
  const ctx = fakeContext([216, 164, 143, 255]);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  expect(cssColor("--a")).toBe("rgb(216, 164, 143)");
  expect(cssColor("var(--a)")).toBe("rgb(216, 164, 143)");
});

test("cssColor кэширует результат и сбрасывает кэш", () => {
  document.documentElement.style.setProperty("--c", "color-mix(in oklab, #d8a48f 80%, black)");
  const ctx = fakeContext([1, 2, 3, 255]);
  const spy = vi
    .spyOn(HTMLCanvasElement.prototype, "getContext")
    .mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  expect(cssColor("--c")).toBe("rgb(1, 2, 3)");
  expect(cssColor("--c")).toBe("rgb(1, 2, 3)");
  expect(ctx.getImageData).toHaveBeenCalledTimes(1);
  clearCssColorCache();
  ctx.getImageData.mockReturnValue({ data: new Uint8ClampedArray([9, 9, 9, 255]) });
  expect(cssColor("--c")).toBe("rgb(9, 9, 9)");
  expect(spy).toHaveBeenCalled();
});

test("cssColor сбрасывает кэш при смене data-theme", () => {
  document.documentElement.style.setProperty("--d", "color-mix(in oklab, #9fbfd3 80%, black)");
  const ctx = fakeContext([5, 5, 5, 255]);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  expect(cssColor("--d")).toBe("rgb(5, 5, 5)");
  ctx.getImageData.mockReturnValue({ data: new Uint8ClampedArray([7, 7, 7, 255]) });
  document.documentElement.setAttribute("data-theme", "dark");
  try {
    expect(cssColor("--d")).toBe("rgb(7, 7, 7)");
  } finally {
    document.documentElement.removeAttribute("data-theme");
  }
});

test("normalizeColor превращает то, что отдал контекст, в rgb(r, g, b)", () => {
  const ctx = fakeContext([12, 34, 56, 255]);
  expect(normalizeColor("color(srgb 0.05 0.13 0.22)", ctx)).toBe("rgb(12, 34, 56)");
  expect(ctx.fillStyle).toBe("color(srgb 0.05 0.13 0.22)");
  expect(ctx.fillRect).toHaveBeenCalledWith(0, 0, 1, 1);
  expect(normalizeColor("oklab(0.5 0.1 0.1)", fakeContext([200, 100, 50, 255]))).toBe(
    "rgb(200, 100, 50)",
  );
});

test("normalizeColor переводит hex в rgb() и без canvas", () => {
  expect(normalizeColor("#d8a48f", null)).toBe("rgb(216, 164, 143)");
  expect(normalizeColor("#abc", null)).toBe("rgb(170, 187, 204)");
  expect(normalizeColor("#D8A48F", fakeContext([216, 164, 143, 255]))).toBe("rgb(216, 164, 143)");
});

test("withAlpha добавляет прозрачность к rgb()", () => {
  expect(withAlpha("rgb(1, 2, 3)", 0.25)).toBe("rgba(1, 2, 3, 0.25)");
});

test("schemeColor берёт вариант текущей темы, без цвета — --t0", () => {
  const pair = { light: "#e08a6c", dark: "#d77755" };
  expect(schemeColor(pair)).toBe("rgb(224, 138, 108)");
  document.documentElement.setAttribute("data-theme", "dark");
  expect(schemeColor(pair)).toBe("rgb(215, 119, 85)");
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.setProperty("--t0", "#bdb6a8");
  expect(schemeColor(undefined)).toBe("rgb(189, 182, 168)");
});

test("без canvas cssColor всё равно отдаёт rgb(): запасной нейтральный --t0", () => {
  document.documentElement.style.setProperty("--e", "color-mix(in oklab, #bdb0d8 80%, black)");
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  expect(cssColor("--e")).toMatch(/^rgb\(\d+, \d+, \d+\)$/);
  expect(normalizeColor("oklab(0.5 0.1 0.1)", null)).toBe("rgb(189, 182, 168)");
  expect(normalizeColor("rgb(1, 2, 3)", null)).toBe("rgb(1, 2, 3)");
});
