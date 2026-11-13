// Клиент HTTP API: сессия — cookie `mb_session` того же origin, ошибки — JSON `{"detail": ...}`.

export type QueryParams = Record<string, string | number | string[] | undefined>;

/** Ошибка API: код ответа и безопасный текст `detail` (он же `message` для показа). */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Ошибка «нет сессии»: показываем подсказку о входе. */
export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** Строка запроса: массив — повтор ключа (`type=a&type=b`), `undefined` пропускается. */
function queryString(params: QueryParams): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    for (const item of Array.isArray(value) ? value : [value]) search.append(key, String(item));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // не JSON (например, страница прокси) — ниже statusText
  }
  return response.statusText || `Ошибка ${response.status}`;
}

export async function apiGet<T>(path: string, params: QueryParams = {}): Promise<T> {
  const response = await fetch(`${path}${queryString(params)}`, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new ApiError(response.status, await errorDetail(response));
  return (await response.json()) as T;
}
