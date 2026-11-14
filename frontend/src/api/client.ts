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

/** Текст ошибки: строковый `detail` сервера, иначе — по коду ответа. */
export function detailOf(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
    return body.detail;
  }
  if (status === 422) return "Проверьте введённые данные";
  if (status === 413) return "Слишком большой запрос";
  if (status >= 500) return "Сервис временно недоступен";
  return "Ошибка запроса";
}

const unauthorizedHandlers = new Set<() => void>();

/** Подписка на ответы 401 (сессия кончилась): возвращает отписку. */
export function onUnauthorized(handler: () => void): () => void {
  unauthorizedHandlers.add(handler);
  return () => {
    unauthorizedHandlers.delete(handler);
  };
}

/** Сообщить подписчикам о 401 — для запросов в обход apiGet/apiSend (поток ответа, загрузка). */
export function notifyUnauthorized(): void {
  for (const handler of [...unauthorizedHandlers]) handler();
}

export type SendOptions = {
  /** 401 здесь — не истёкшая сессия (неверный пароль): подписчиков не будить. */
  quiet401?: boolean;
  /** Отмена запроса: `fetch` бросит `AbortError`, он пробрасывается как есть. */
  signal?: AbortSignal;
};

/** Ошибка из неуспешного ответа; при 401 (если не `quiet401`) — уведомление подписчиков. */
export async function errorFromResponse(response: Response, options: SendOptions = {}): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // не JSON (например, страница прокси) — текст по коду ответа
  }
  if (response.status === 401 && !options.quiet401) notifyUnauthorized();
  return new ApiError(response.status, detailOf(body, response.status));
}

/** fetch с переводом сетевого сбоя в `ApiError(0, …)`; отмену (`AbortError`) пробрасываем как есть. */
async function request(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, { credentials: "same-origin", ...init });
  } catch (error) {
    if ((error as { name?: unknown } | null)?.name === "AbortError") throw error;
    throw new ApiError(0, "Нет связи с сервером");
  }
}

export async function apiGet<T>(path: string, params: QueryParams = {}): Promise<T> {
  const response = await request(`${path}${queryString(params)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

/** Запрос с изменением: JSON-тело — только если `json` передан; 204 → `null`. */
export async function apiSend<T>(
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  json?: unknown,
  options: SendOptions = {},
): Promise<T | null> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers };
  if (options.signal) init.signal = options.signal;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  const response = await request(path, init);
  if (!response.ok) throw await errorFromResponse(response, options);
  if (response.status === 204) return null;
  return (await response.json()) as T;
}
