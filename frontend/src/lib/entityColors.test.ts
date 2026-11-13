import { afterEach, expect, test, vi } from "vitest";
import {
  clearCssColorCache,
  cssColor,
  mixRgb,
  normalizeColor,
  schemeColor,
  typeColorRgb,
  withAlpha,
} from "./entityColors";
import { typeColorAt } from "./typePalette";

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

test("typeColorRgb: цвет типа по позиции в списке для текущей темы", () => {
  const hex = (h: string) => `rgb(${parseInt(h.slice(1, 3), 16)}, ${parseInt(h.slice(3, 5), 16)}, ${parseInt(h.slice(5, 7), 16)})`;
  expect(typeColorRgb("Org", ["Person", "Org"])).toBe(hex(typeColorAt(1).light));
  document.documentElement.setAttribute("data-theme", "dark");
  expect(typeColorRgb("Org", ["Person", "Org"])).toBe(hex(typeColorAt(1).dark));
  document.documentElement.removeAttribute("data-theme");
});

test("typeColorRgb: 15 типов — 15 разных цветов, без повторов по кругу", () => {
  const types = Array.from({ length: 15 }, (_, i) => `T${i}`);
  expect(new Set(types.map((t) => typeColorRgb(t, types))).size).toBe(15);
});

test("неизвестный тип и «Другое» получают нейтральный --t0", () => {
  document.documentElement.style.setProperty("--t0", "#bdb6a8");
  expect(typeColorRgb("Nope", ["Person"])).toBe("rgb(189, 182, 168)");
  expect(typeColorRgb("Other", ["Person", "Other"])).toBe("rgb(189, 182, 168)");
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

test("mixRgb смешивает два rgb() по доле второго цвета; чужие строки — первый как есть", () => {
  expect(mixRgb("rgb(0, 0, 0)", "rgb(200, 100, 50)", 0.5)).toBe("rgb(100, 50, 25)");
  expect(mixRgb("rgb(10, 20, 30)", "rgb(200, 100, 50)", 0)).toBe("rgb(10, 20, 30)");
  expect(mixRgb("rgb(10, 20, 30)", "rgb(200, 100, 50)", 1)).toBe("rgb(200, 100, 50)");
  expect(mixRgb("red", "rgb(1, 2, 3)", 0.5)).toBe("red");
});
