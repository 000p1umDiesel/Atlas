import { ApiError, onUnauthorized } from "./client";
import { parseSse, streamSse } from "./sse";

it("parses events across chunk boundaries", () => {
  const first = parseSse('event: delta\ndata: {"text":"При');
  expect(first.events).toEqual([]);
  const second = parseSse(first.rest + 'вет"}\n\nevent: done\ndata: {"id":"m1"}\n\n: comment\n\n');
  expect(second.events).toEqual([{ event: "delta", data: { text: "Привет" } }, { event: "done", data: { id: "m1" } }]);
  expect(second.rest).toBe("");
});

it("joins multi-line data and defaults the event name", () => {
  expect(parseSse('data: {"a":\ndata: 1}\n\n').events).toEqual([{ event: "message", data: { a: 1 } }]);
});

/** Тело ответа из заранее заданных кусков байтов. */
function body(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

const fetchMock = vi.fn();
beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

it("streamSse posts JSON, asks for an event stream and decodes split UTF-8", async () => {
  const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"Привет"}\n\nevent: done\ndata: {"id":"m1"}\n\n');
  // Разрез посреди двухбайтовой буквы: декодер в потоковом режиме склеивает её.
  const cut = bytes.indexOf(0xd0) + 1;
  fetchMock.mockResolvedValueOnce(new Response(body([bytes.slice(0, cut), bytes.slice(cut)])));
  const events: [string, unknown][] = [];
  const opened = vi.fn();
  await streamSse("/api/conversations/c1/messages/stream", { question: "Кто?", mode: "rag" }, (event, data) => {
    events.push([event, data]);
  }, undefined, opened);

  expect(events).toEqual([
    ["delta", { text: "Привет" }],
    ["done", { id: "m1" }],
  ]);
  expect(opened).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe("/api/conversations/c1/messages/stream");
  expect(init.method).toBe("POST");
  expect(init.credentials).toBe("same-origin");
  expect(JSON.parse(init.body)).toEqual({ question: "Кто?", mode: "rag" });
  const headers = new Headers(init.headers);
  expect(headers.get("Content-Type")).toBe("application/json");
  expect(headers.get("Accept")).toBe("text/event-stream");
});

it("streamSse turns a non-2xx response into ApiError and does not open the stream", async () => {
  fetchMock.mockResolvedValueOnce(Response.json({ detail: "Дневной лимит токенов исчерпан" }, { status: 429 }));
  const onEvent = vi.fn();
  const opened = vi.fn();
  const result = streamSse("/api/x", {}, onEvent, undefined, opened);
  await expect(result).rejects.toBeInstanceOf(ApiError);
  await expect(result).rejects.toMatchObject({ status: 429, detail: "Дневной лимит токенов исчерпан" });
  expect(onEvent).not.toHaveBeenCalled();
  expect(opened).not.toHaveBeenCalled();
});

it("streamSse: 401 notifies the session handlers", async () => {
  const handler = vi.fn();
  const off = onUnauthorized(handler);
  fetchMock.mockResolvedValueOnce(Response.json({ detail: "Требуется вход" }, { status: 401 }));
  await expect(streamSse("/api/x", {}, () => {})).rejects.toMatchObject({ status: 401 });
  expect(handler).toHaveBeenCalledTimes(1);
  off();
});

it("streamSse: network failure is ApiError(0), abort is rethrown", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await expect(streamSse("/api/x", {}, () => {})).rejects.toMatchObject({ status: 0, detail: "Нет связи с сервером" });
  fetchMock.mockRejectedValueOnce(new DOMException("aborted", "AbortError"));
  await expect(streamSse("/api/x", {}, () => {})).rejects.toMatchObject({ name: "AbortError" });
});

it("streamSse: a broken stream is ApiError(0)", async () => {
  let reads = 0;
  const broken = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (reads++ === 0) controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"text":"a"}\n\n'));
      else controller.error(new TypeError("network error"));
    },
  });
  fetchMock.mockResolvedValueOnce(new Response(broken));
  const onEvent = vi.fn();
  await expect(streamSse("/api/x", {}, onEvent)).rejects.toMatchObject({ status: 0, detail: "Нет связи с сервером" });
  expect(onEvent).toHaveBeenCalledWith("delta", { text: "a" });
});

/** Тело ответа, у читателя которого отслеживается `cancel` (поток закрыт клиентом). */
function watched(stream: ReadableStream<Uint8Array>) {
  const cancel = vi.fn(() => Promise.reject(new Error("cancel failed"))); // ошибка отмены не мешает
  const original = stream.getReader.bind(stream);
  stream.getReader = (() => {
    const reader = original();
    reader.cancel = cancel;
    return reader;
  }) as typeof stream.getReader;
  // Ответ-заглушка: `new Response` может обернуть поток, и подмена getReader потеряется.
  return { response: { ok: true, status: 200, body: stream } as unknown as Response, cancel };
}

it("streamSse: broken JSON in an event is ApiError(502) and the stream is cancelled", async () => {
  const { response, cancel } = watched(
    body([new TextEncoder().encode('event: delta\ndata: {"text":"a"}\n\nevent: delta\ndata: {broken\n\n')]),
  );
  fetchMock.mockResolvedValueOnce(response);
  const onEvent = vi.fn();
  const result = streamSse("/api/x", {}, onEvent);
  await expect(result).rejects.toBeInstanceOf(ApiError);
  await expect(result).rejects.toMatchObject({ status: 502, detail: "Неожиданный ответ сервера" });
  expect(cancel).toHaveBeenCalledTimes(1);
});

it("streamSse: a read failure mid-stream cancels the reader and is ApiError(0)", async () => {
  let reads = 0;
  const { response, cancel } = watched(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        if (reads++ === 0) controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"text":"a"}\n\n'));
        else controller.error(new TypeError("network error"));
      },
    }),
  );
  fetchMock.mockResolvedValueOnce(response);
  await expect(streamSse("/api/x", {}, () => {})).rejects.toMatchObject({ status: 0, detail: "Нет связи с сервером" });
  expect(cancel).toHaveBeenCalledTimes(1);
});
