import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { Conversation, Message, Source } from "../api/types";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { deferred, fail, fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };

const conv = (id: string, title: string): Conversation => ({
  id,
  user_id: "u1",
  title,
  created_at: "2026-11-14T10:00:00Z",
});

function msg(over: Partial<Message>): Message {
  return {
    id: "m",
    conversation_id: "c1",
    user_id: "u1",
    role: "user",
    content: "",
    mode: "rag",
    sources: [],
    status: "done",
    query: null,
    meta: null,
    created_at: "2026-11-14T10:00:00Z",
    ...over,
  };
}

const SOURCE: Source = {
  n: 1,
  kind: "chunk",
  ref: "0123456789abcdef:00003",
  path: "smith.pdf",
  page: 3,
  snippet: "Адам Смит — шотландский экономист",
  cited: true,
  doc_id: "0123456789abcdef",
};

const HISTORY: Message[] = [
  msg({ id: "m1", role: "user", content: "Кто такой Смит?" }),
  msg({ id: "m2", role: "assistant", content: "Шотландский экономист." }),
];

const SAVED = msg({
  id: "m4",
  role: "assistant",
  content: "Ответ готов [1].",
  sources: [SOURCE],
  meta: { latency_ms: 2000, tokens_in: 50, tokens_out: 10, alt_queries: [] },
});

const MESSAGES = (id: string) => `GET /api/conversations/${id}/messages?limit=200&newest=true`;
const STREAM = (id: string) => `POST /api/conversations/${id}/messages/stream`;
const CONVERSATIONS = "GET /api/conversations?limit=200";

type Routes = Parameters<typeof fakeApi>[0];

function routes(extra: Routes = {}): Routes {
  return {
    "GET /api/auth/me": ME,
    [CONVERSATIONS]: [conv("c1", "Про Смита"), conv("c2", "")],
    [MESSAGES("c1")]: HISTORY,
    [MESSAGES("c2")]: [msg({ id: "x1", conversation_id: "c2", content: "Вопрос во втором" })],
    ...extra,
  };
}

/** Поток событий ответа, которые тест шлёт сам. */
function sseStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: () => new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
    async emit(event: string, data: unknown) {
      await act(async () => {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
        await tick();
      });
    },
    async close() {
      await act(async () => {
        controller.close();
        await tick();
      });
    },
    /** Обрыв соединения посреди чтения. */
    async fail() {
      await act(async () => {
        controller.error(new TypeError("network error"));
        await tick();
      });
    },
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  return router;
}

const box = () => screen.getByRole("textbox", { name: "Вопрос" }) as HTMLTextAreaElement;
const count = (api: FakeApi, request: string) => api.requests().filter((r) => r === request).length;
const column = () => screen.getByRole("complementary", { name: "Контекст раздела" });

async function ask(question: string) {
  const field = await screen.findByRole("textbox", { name: "Вопрос" });
  fireEvent.change(field, { target: { value: question } });
  await act(async () => {
    fireEvent.keyDown(field, { key: "Enter" });
    await tick();
  });
}

/** Ответ закончен: кнопка снова «Спросить». */
const idle = () => vi.waitFor(() => expect(screen.getByRole("button", { name: /Спросить/ })).toBeTruthy());

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("sources → delta ×2 → done: одно сообщение ассистента с полным текстом и метриками", async () => {
  const stream = sseStream();
  const opened = deferred<Response>();
  const api = fakeApi(routes({ [STREAM("c1")]: () => opened.promise }));
  await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");

  await ask("Что он написал?");
  expect(api.bodies("POST", "/api/conversations/c1/messages/stream")).toEqual([
    { question: "Что он написал?", mode: "rag" },
  ]);
  // Вопрос и пустой ответ — сразу; поле не очищается, пока сервер не принял вопрос.
  expect(await screen.findByText("Что он написал?", { selector: "p" })).toBeTruthy();
  expect(screen.getByText("Ищу в документах и пишу ответ…")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Отвечаю…" })).toBeTruthy();
  expect(box().value).toBe("Что он написал?");

  await act(async () => {
    opened.resolve(stream.response());
    await tick();
  });
  expect(box().value).toBe("");

  await stream.emit("sources", [SOURCE]);
  await stream.emit("delta", { text: "Ответ " });
  await stream.emit("delta", { text: "готов [1]." });
  await vi.waitFor(() => expect(screen.getByText(/Ответ готов/)).toBeTruthy());
  expect(screen.getByRole("button", { name: "Отвечаю…" })).toBeTruthy();

  await stream.emit("done", SAVED);
  await stream.close();
  await idle();
  expect(screen.getAllByText(/Ответ готов/)).toHaveLength(1);
  expect(screen.getByText(/50 \+ 10 = 60 токенов/)).toBeTruthy();
  expect(screen.queryByText("Ищу в документах и пишу ответ…")).toBeNull();
  // История не перечитывается, список диалогов — да (название ставит сервер).
  expect(count(api, MESSAGES("c1"))).toBe(1);
  await vi.waitFor(() => expect(count(api, CONVERSATIONS)).toBe(2));
});

test("событие error — сообщение-ошибка с detail", async () => {
  const stream = sseStream();
  const api = fakeApi(routes({ [STREAM("c1")]: () => stream.response() }));
  await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Вопрос");
  await stream.emit("sources", []);
  await stream.emit("error", { detail: "Модель или база знаний недоступны", status: 503 });
  await stream.close();
  await idle();
  expect(screen.getByText("Модель или база знаний недоступны")).toBeTruthy();
  expect(screen.queryByText("Ищу в документах и пишу ответ…")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("событие error без detail — непустой текст «Не удалось получить ответ»", async () => {
  const stream = sseStream();
  const api = fakeApi(routes({ [STREAM("c1")]: () => stream.response() }));
  await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Вопрос");
  await stream.emit("error", { status: 500 });
  await stream.close();
  await idle();
  expect(screen.getByText("Не удалось получить ответ")).toBeTruthy();
});

test("обрыв чтения посреди ответа — пузырь больше не «отвечает», история перечитывается", async () => {
  const stream = sseStream();
  // Перечитанная история ещё не пришла: пузырь в кэше остаётся и не должен «отвечать» вечно.
  const reloaded = deferred<Response>();
  let loads = 0;
  const api = fakeApi(
    routes({
      [STREAM("c1")]: () => stream.response(),
      [MESSAGES("c1")]: () => (loads++ === 0 ? Response.json(HISTORY) : reloaded.promise),
    }),
  );
  await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Оборванный вопрос");
  await stream.emit("delta", { text: "Начало" });
  await stream.fail();
  await idle();
  await vi.waitFor(() => expect(count(api, MESSAGES("c1"))).toBe(2));
  expect((await screen.findByRole("alert")).textContent).toBe("Нет связи с сервером");
  const bubble = screen.getByText("Начало").closest("[aria-busy]")!;
  expect(bubble.getAttribute("aria-busy")).toBe("false");
  expect(document.querySelector('[aria-busy="true"]')).toBeNull();
});

test("поток оборвался без done/error — история перечитывается, «Отвечаю…» пропадает", async () => {
  const stream = sseStream();
  const api = fakeApi(routes({ [STREAM("c1")]: () => stream.response() }));
  await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Оборванный вопрос");
  await stream.emit("delta", { text: "Начало" });
  await stream.close();
  await idle();
  await vi.waitFor(() => expect(count(api, MESSAGES("c1"))).toBe(2));
  // С сервера пришла история без недописанного ответа.
  await vi.waitFor(() => expect(screen.queryByText("Начало")).toBeNull());
  expect(screen.queryByText("Ищу в документах и пишу ответ…")).toBeNull();
});

test("429 до потока — текст detail над полем, вопрос остаётся в поле, busy снова false", async () => {
  const api = fakeApi(routes({ [STREAM("c1")]: () => fail(429, "Дневной лимит токенов исчерпан") }));
  await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Лишний вопрос");
  expect((await screen.findByRole("alert")).textContent).toBe("Дневной лимит токенов исчерпан");
  await idle();
  expect(box().value).toBe("Лишний вопрос");
  expect((screen.getByRole("button", { name: /Спросить/ }) as HTMLButtonElement).disabled).toBe(false);
  // Оптимистичные сообщения убраны, история перечитана.
  expect(screen.queryByText("Лишний вопрос", { selector: "p" })).toBeNull();
  expect(screen.queryByText("Ищу в документах и пишу ответ…")).toBeNull();
  await vi.waitFor(() => expect(count(api, MESSAGES("c1"))).toBe(2));
});

test("401 до потока — переход на вход с возвратом в диалог", async () => {
  const api = fakeApi(routes({ [STREAM("c1")]: () => fail(401, "Требуется вход") }));
  const router = await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Вопрос");
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  expect(router.state.location.search).toEqual({ next: "/chat/c1" });
});

test("смена диалога посреди потока: события не попадают в открытый диалог", async () => {
  const stream = sseStream();
  const api = fakeApi(routes({ [STREAM("c1")]: () => stream.response() }));
  const router = await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Вопрос первому");
  await stream.emit("sources", [SOURCE]);

  await act(async () => {
    fireEvent.click(await within(column()).findByRole("link", { name: "Без названия" }));
    await tick();
  });
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat/c2"));
  await screen.findByText("Вопрос во втором");

  await stream.emit("delta", { text: "Ответ первому" });
  await stream.emit("done", { ...SAVED, content: "Ответ первому" });
  await stream.close();
  await idle();
  expect(screen.queryByText("Ответ первому")).toBeNull();
  expect(screen.queryByText("Вопрос первому", { selector: "p" })).toBeNull();
  expect(screen.getByText("Вопрос во втором")).toBeTruthy();

  // Ответ сохранён у своего диалога.
  await act(async () => {
    fireEvent.click(within(column()).getByRole("link", { name: "Про Смита" }));
    await tick();
  });
  expect(await screen.findByText("Ответ первому")).toBeTruthy();
});

test("первый вопрос без диалога: POST /api/conversations, URL /chat/<id>, история не перечитывается", async () => {
  const stream = sseStream();
  const api = fakeApi(
    routes({
      "POST /api/conversations": () => Response.json(conv("c9", ""), { status: 201 }),
      [STREAM("c9")]: () => stream.response(),
      [MESSAGES("c9")]: [],
    }),
  );
  const router = await renderAt("/chat", api);
  await ask("Первый вопрос");
  expect(api.bodies("POST", "/api/conversations")).toEqual([{}]);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat/c9"));
  expect(await screen.findByText("Первый вопрос", { selector: "p" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Отвечаю…" })).toBeTruthy();

  await stream.emit("delta", { text: "Ответ готов [1]." });
  await stream.emit("done", { ...SAVED, conversation_id: "c9" });
  await stream.close();
  await idle();
  expect(screen.getAllByText(/Ответ готов/)).toHaveLength(1);
  expect(count(api, MESSAGES("c9"))).toBe(0);
});

test("первый вопрос: сервер не принял вопрос — диалог открыт, ошибка и вопрос в поле", async () => {
  const api = fakeApi(
    routes({
      "POST /api/conversations": () => Response.json(conv("c9", ""), { status: 201 }),
      [STREAM("c9")]: () => fail(429, "Дневной лимит токенов исчерпан"),
      [MESSAGES("c9")]: [],
    }),
  );
  const router = await renderAt("/chat", api);
  await ask("Первый вопрос");
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat/c9"));
  expect((await screen.findByRole("alert")).textContent).toBe("Дневной лимит токенов исчерпан");
  await idle();
  expect(box().value).toBe("Первый вопрос");
});

test("диалог не создался — ошибка на /chat, вопрос остаётся в поле", async () => {
  const api = fakeApi(routes({ "POST /api/conversations": () => fail(503, "Сервис временно недоступен") }));
  const router = await renderAt("/chat", api);
  await ask("Первый вопрос");
  expect((await screen.findByRole("alert")).textContent).toBe("Сервис временно недоступен");
  await idle();
  expect(router.state.location.pathname).toBe("/chat");
  expect(box().value).toBe("Первый вопрос");
});

async function logIn() {
  fireEvent.change(await screen.findByLabelText("Имя пользователя"), { target: { value: "owner" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "correct horse battery" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await tick();
  });
}

const AUTH = { "POST /api/auth/login": ME, "POST /api/auth/logout": { status: "ok" } };

test("401 до потока → вход → снова в диалоге: прежней ошибки нет", async () => {
  const api = fakeApi(routes({ ...AUTH, [STREAM("c1")]: () => fail(401, "Требуется вход") }));
  const router = await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Вопрос");
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));

  await logIn();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat/c1"));
  await screen.findByText("Кто такой Смит?");
  await idle();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("401 при создании диалога → вход → /chat без прежней ошибки", async () => {
  const api = fakeApi(routes({ ...AUTH, "POST /api/conversations": () => fail(401, "Требуется вход") }));
  const router = await renderAt("/chat", api);
  await ask("Первый вопрос");
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));

  await logIn();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
  await screen.findByRole("heading", { name: "О чём спросить?" });
  expect(screen.queryByRole("alert")).toBeNull();
});

test("ошибка отправки не переживает выход и новый вход", async () => {
  const api = fakeApi(routes({ ...AUTH, [STREAM("c1")]: () => fail(429, "Дневной лимит токенов исчерпан") }));
  const router = await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Лишний вопрос");
  await screen.findByRole("alert");
  await idle();

  await act(async () => {
    fireEvent.click(within(screen.getByRole("navigation", { name: "Разделы" })).getByRole("button", { name: "Выйти" }));
    await tick();
  });
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  await logIn();
  await act(async () => router.navigate({ to: "/chat/$id", params: { id: "c1" } }));
  await screen.findByText("Кто такой Смит?");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(box().value).toBe("");
});

test("поток прежней сессии после выхода и входа не трогает новую", async () => {
  const stream = sseStream();
  const api = fakeApi(routes({ ...AUTH, [STREAM("c1")]: () => stream.response() }));
  const router = await renderAt("/chat/c1", api);
  await screen.findByText("Кто такой Смит?");
  await ask("Вопрос до выхода");
  await screen.findByRole("button", { name: "Отвечаю…" });

  await act(async () => {
    fireEvent.click(within(screen.getByRole("navigation", { name: "Разделы" })).getByRole("button", { name: "Выйти" }));
    await tick();
  });
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  await logIn();
  await act(async () => router.navigate({ to: "/chat/$id", params: { id: "c1" } }));
  await screen.findByText("Кто такой Смит?");
  // Новая сессия: можно спрашивать, недописанного ответа прежней нет.
  expect(screen.getByRole("button", { name: /Спросить/ })).toBeTruthy();
  expect(screen.queryByText("Вопрос до выхода", { selector: "p" })).toBeNull();

  await stream.emit("delta", { text: "Старый ответ" });
  await stream.emit("done", { ...SAVED, content: "Старый ответ" });
  await stream.close();
  expect(screen.queryByText("Старый ответ")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("button", { name: /Спросить/ })).toBeTruthy();
});
