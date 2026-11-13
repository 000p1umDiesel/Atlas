const PALETTE_SIZE = 8;

/** CSS-переменная цвета типа: по индексу в списке настроенных типов, циклически по 8. */
export function typeColorVar(type: string, configured: string[]): string {
  const index = configured.indexOf(type);
  if (index < 0) return "var(--t0)";
  return `var(--t${(index % PALETTE_SIZE) + 1})`;
}

/**
 * Конкретный цвет для WebGL (там нельзя передать var() или color-mix()).
 * Берём значение переменной с <html>; вложенный var() разворачиваем рекурсивно;
 * остальное (hex, color-mix) прогоняем через временный элемент: браузер отдаёт
 * вычисленный `color`.
 */
export function cssColor(varName: string, depth = 0): string {
  const name = varName.replace(/^var\(\s*/, "").replace(/\s*\)$/, "");
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const nested = /^var\(\s*(--[\w-]+)\s*\)$/.exec(raw);
  if (nested && depth < 8) return cssColor(nested[1], depth + 1);
  if (/^#[0-9a-f]{3,8}$/i.test(raw)) return raw;
  const probe = document.createElement("span");
  probe.style.color = raw;
  document.body.appendChild(probe);
  const resolved = getComputedStyle(probe).color;
  probe.remove();
  return resolved || raw;
}
