import type { ThemedColor } from "./communityPalette";

const PALETTE_SIZE = 8;

/** CSS-переменная цвета типа: по индексу в списке настроенных типов, циклически по 8. */
export function typeColorVar(type: string, configured: string[]): string {
  const index = configured.indexOf(type);
  if (index < 0) return "var(--t0)";
  return `var(--t${(index % PALETTE_SIZE) + 1})`;
}

/** Минимум 2D-контекста, нужный для нормализации (его подменяют в тестах: в jsdom нет canvas). */
export interface ColorContext {
  fillStyle: string | CanvasGradient | CanvasPattern;
  clearRect(x: number, y: number, w: number, h: number): void;
  fillRect(x: number, y: number, w: number, h: number): void;
  getImageData(x: number, y: number, w: number, h: number): { data: ArrayLike<number> };
}

function hexToRgb(value: string): string | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec(value.trim());
  if (!m) return null;
  const hex = m[1].length === 3 ? [...m[1]].map((c) => c + c).join("") : m[1];
  const n = Number.parseInt(hex, 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

/** Нейтральный `--t0` светлой темы: запасной цвет, если canvas недоступен. */
const FALLBACK_RGB = "rgb(189, 182, 168)";

/**
 * Любой CSS-цвет → `rgb(r, g, b)`, который понимают WebGL-рендереры.
 * Hex разбираем сами; остальное (`color(srgb …)`, `oklab(…)`, `color-mix(…)`)
 * рисуем в пиксель 1×1 и читаем обратно. Без canvas — готовый `rgb()` как есть,
 * иначе нейтральный цвет (никогда не строка, которую WebGL не разберёт).
 */
export function normalizeColor(value: string, ctx: ColorContext | null): string {
  const hex = hexToRgb(value);
  if (hex) return hex;
  if (!ctx) return /^rgb\(\s*\d+,\s*\d+,\s*\d+\s*\)$/.test(value.trim()) ? value.trim() : FALLBACK_RGB;
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = "#000000";
  ctx.fillStyle = value;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = Array.from(ctx.getImageData(0, 0, 1, 1).data);
  return `rgb(${r}, ${g}, ${b})`;
}

/** `rgb(r, g, b)` → `rgba(r, g, b, a)`; иные строки возвращаются как есть. */
export function withAlpha(rgb: string, alpha: number): string {
  const m = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/.exec(rgb);
  if (!m) return rgb;
  return `rgba(${m[1]}, ${m[2]}, ${m[3]}, ${alpha})`;
}

/** Смесь двух `rgb(r, g, b)`: доля `t` второго цвета (0 — первый, 1 — второй). */
export function mixRgb(a: string, b: string, t: number): string {
  const re = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/;
  const p = re.exec(a);
  const q = re.exec(b);
  if (!p || !q) return a;
  const k = Math.min(1, Math.max(0, t));
  const ch = (i: number) => Math.round(Number(p[i]) * (1 - k) + Number(q[i]) * k);
  return `rgb(${ch(1)}, ${ch(2)}, ${ch(3)})`;
}

let colorContext: ColorContext | null | undefined;

function getColorContext(): ColorContext | null {
  if (colorContext === undefined) {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    colorContext = canvas.getContext("2d", { willReadFrequently: true });
  }
  return colorContext;
}

const cache = new Map<string, string>();
let cacheTheme = "";

/** Признак темы: ручной выбор или системная настройка. Сменился — кэш цветов устарел. */
function themeSignature(): string {
  const manual = document.documentElement.getAttribute("data-theme");
  if (manual) return manual;
  const dark = typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches;
  return dark ? "system-dark" : "system-light";
}

/** Сейчас действует тёмная тема (ручной выбор или системная настройка). */
export function isDarkScheme(): boolean {
  const theme = themeSignature();
  return theme === "dark" || theme === "system-dark";
}

/** Цвет из пары «светлая/тёмная» для WebGL: `rgb(r, g, b)` текущей темы; нет цвета — `--t0`. */
export function schemeColor(color: ThemedColor | undefined): string {
  if (!color) return cssColor("--t0");
  return hexToRgb(isDarkScheme() ? color.dark : color.light) ?? FALLBACK_RGB;
}

/** Сброс кэша (смена темы; тесты). */
export function clearCssColorCache(): void {
  cache.clear();
  colorContext = undefined;
}

function resolveVar(varName: string, depth: number): string {
  const name = varName.replace(/^var\(\s*/, "").replace(/\s*\)$/, "");
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const nested = /^var\(\s*(--[\w-]+)\s*\)$/.exec(raw);
  if (nested && depth < 8) return resolveVar(nested[1], depth + 1);
  if (hexToRgb(raw)) return raw;
  // color-mix() и прочее: браузер отдаёт вычисленный `color` временного элемента.
  const probe = document.createElement("span");
  probe.style.color = raw;
  document.body.appendChild(probe);
  const resolved = getComputedStyle(probe).color;
  probe.remove();
  return resolved || raw;
}

/**
 * Конкретный цвет для WebGL (там нельзя передать var() или color-mix()):
 * всегда `rgb(r, g, b)`. Результат кэшируется по имени переменной до смены темы.
 */
export function cssColor(varName: string): string {
  const theme = themeSignature();
  if (theme !== cacheTheme) {
    cache.clear();
    cacheTheme = theme;
  }
  const hit = cache.get(varName);
  if (hit !== undefined) return hit;
  const raw = resolveVar(varName, 0);
  const color = hexToRgb(raw) ?? normalizeColor(raw, getColorContext());
  cache.set(varName, color);
  return color;
}
