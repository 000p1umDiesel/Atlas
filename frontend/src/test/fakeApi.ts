// Подмена fetch для тестов экранов: ответы по ключу «МЕТОД /путь» (сначала с query, потом без).

type Reply = Response | Promise<Response>;
/** Обработчик: тело запроса (разобранный JSON или undefined) и полный URL. */
type Handler = (body: unknown, url: string) => Reply;

export type FakeApi = ReturnType<typeof vi.fn<(url: string, init?: RequestInit) => Promise<Response>>> & {
  /** Сделанные запросы: «МЕТОД url». */
  requests: () => string[];
  /** JSON-тела запросов к пути (в порядке вызова). */
  bodies: (method: string, path: string) => unknown[];
};

/** Ответ с ошибкой в форме API. */
export function fail(status: number, detail: string): Response {
  return Response.json({ detail }, { status });
}

/** Значение-объект отдаётся как JSON 200; функция — как обработчик; неизвестный путь — 404. */
export function fakeApi(routes: Record<string, Handler | object>): FakeApi {
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = url.split("?")[0]!;
    const route = routes[`${method} ${url}`] ?? routes[`${method} ${path}`];
    if (route === undefined) return fail(404, "Не найдено");
    if (typeof route !== "function") return Response.json(route);
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    return (route as Handler)(body, url);
  }) as FakeApi;
  const calls = () => mock.mock.calls.map(([url, init]) => ({ url, method: init?.method ?? "GET", init }));
  mock.requests = () => calls().map(({ method, url }) => `${method} ${url}`);
  mock.bodies = (method, path) =>
    calls()
      .filter((call) => call.method === method && call.url.split("?")[0] === path)
      .map((call) => (typeof call.init?.body === "string" ? JSON.parse(call.init.body) : undefined));
  return mock;
}

/** Обещание, которое тест выполняет сам: проверить состояние «запрос идёт». */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
