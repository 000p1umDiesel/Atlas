/**
 * Search-параметры без JSON. Стандартный парсер TanStack превращает «123» в число и «true»
 * в булево, а сериализатор пишет массивы и строки в JSON-кавычках. Здесь значения в URL —
 * всегда простые строки; типы задаёт `validateSearch` маршрута.
 */
export function parsePlainSearch(searchStr: string): Record<string, string | string[]> {
  const params = new URLSearchParams(searchStr.startsWith("?") ? searchStr.slice(1) : searchStr);
  const out: Record<string, string | string[]> = {};
  for (const [key, value] of params) {
    const prev = out[key];
    out[key] = prev === undefined ? value : Array.isArray(prev) ? [...prev, value] : [prev, value];
  }
  return out;
}

function plain(value: unknown): string {
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function stringifyPlainSearch(search: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(search)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) if (item !== undefined && item !== null) params.append(key, plain(item));
    } else {
      params.set(key, plain(value));
    }
  }
  // Запятая в query допустима: списки типов читаются как есть (`types=Person,Concept`).
  const str = params.toString().replace(/%2C/gi, ",");
  return str ? `?${str}` : "";
}
