// Поток событий сервера (SSE) поверх fetch: у EventSource нет POST и тела запроса.
import { ApiError, errorFromResponse } from "./client";

export type SseEvent = { event: string; data: unknown };

/** Значение строки поля: после «поле:» допускается один пробел. */
function fieldValue(line: string, field: string): string | undefined {
  if (!line.startsWith(`${field}:`)) return undefined;
  const value = line.slice(field.length + 1);
  return value.startsWith(" ") ? value.slice(1) : value;
}

/**
 * Разбор накопленного текста потока: блоки по пустой строке, `event:` (по умолчанию `message`),
 * `data:` (несколько строк склеиваются `\n`, затем JSON). Блоки без `data` (комментарии)
 * пропускаются; неполный последний блок возвращается в `rest`.
 */
export function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  let rest = buffer;
  let cut: number;
  while ((cut = rest.indexOf("\n\n")) >= 0) {
    const block = rest.slice(0, cut);
    rest = rest.slice(cut + 2);
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      const name = fieldValue(line, "event");
      if (name !== undefined) event = name;
      const value = fieldValue(line, "data");
      if (value !== undefined) data.push(value);
    }
    if (data.length > 0) events.push({ event, data: JSON.parse(data.join("\n")) });
  }
  return { events, rest };
}

const OFFLINE = "Нет связи с сервером";
const MALFORMED = "Неожиданный ответ сервера";

function isAbort(error: unknown): boolean {
  return (error as { name?: unknown } | null)?.name === "AbortError";
}

/**
 * `POST` с JSON и ответом потоком событий. Ответ не-2xx — `ApiError` (как у `apiSend`; 401
 * будит подписчиков сессии), сетевой сбой и обрыв чтения — `ApiError(0, …)`, отмена
 * (`AbortError`) — как есть, испорченный JSON события — `ApiError(502, …)`. При обрыве и
 * испорченных данных поток закрывается (`reader.cancel()`). `onOpen` — сервер принял запрос
 * (ответ 2xx, поток начался). Завершается, когда сервер закрыл поток.
 */
export async function streamSse(
  path: string,
  body: unknown,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
  onOpen?: () => void,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (isAbort(error)) throw error;
    throw new ApiError(0, OFFLINE);
  }
  if (!response.ok) throw await errorFromResponse(response);
  onOpen?.();
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // Закрыть поток перед броском: соединение не висит; ошибка самой отмены не важна.
  const cancel = () => {
    void reader.cancel().catch(() => {});
  };
  let buffer = "";
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (error) {
      cancel();
      if (isAbort(error)) throw error;
      throw new ApiError(0, OFFLINE);
    }
    if (chunk.done) break;
    let parsed: ReturnType<typeof parseSse>;
    try {
      // Потоковый режим: буква, разрезанная между кусками, склеивается со следующим.
      parsed = parseSse(buffer + decoder.decode(chunk.value, { stream: true }));
    } catch {
      cancel();
      throw new ApiError(502, MALFORMED);
    }
    buffer = parsed.rest;
    for (const { event, data } of parsed.events) onEvent(event, data);
  }
}
