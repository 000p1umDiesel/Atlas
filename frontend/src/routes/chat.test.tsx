import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { Conversation, Message, Source } from "../api/types";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const CHUNK_ID = "0123456789abcdef:00003";

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
  ref: CHUNK_ID,
  path: "smith.pdf",
  page: 3,
  snippet: "Адам Смит — шотландский экономист",
  cited: true,
  doc_id: "0123456789abcdef",
};

const HISTORY: Message[] = [
  msg({ id: "m1", role: "user", content: "Кто такой Смит?", mode: "graph" }),
  msg({
    id: "m2",
    role: "assistant",
    content: "Шотландский экономист [1].",
    mode: "graph",
    sources: [SOURCE],
    meta: { latency_ms: 1500, tokens_in: 100, tokens_out: 20, alt_queries: [] },
  }),
];

const MESSAGES = (id: string) => `GET /api/conversations/${id}/messages?limit=200&newest=true`;

type Routes = Parameters<typeof fakeApi>[0];

function routes(extra: Routes = {}, history: Message[] = HISTORY): Routes {
  return {
    "GET /api/auth/me": ME,
    "GET /api/conversations?limit=200": [conv("c1", "Про Смита"), conv("c2", "")],
    [MESSAGES("c1")]: history,
    ...extra,
  };
}

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  return router;
}

/** Клик, после которого запрос или переход идут асинхронно. */
async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
    await new Promise((r) => setTimeout(r, 0));
  });
}

const column = () => screen.getByRole("complementary", { name: "Контекст раздела" });
const count = (api: FakeApi, request: string) => api.requests().filter((r) => r === request).length;

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("/chat/c1: список диалогов, история, режим вопроса и метрики ответа", async () => {
  await renderAt("/chat/c1", fakeApi(routes()));
  expect(await screen.findByText("Кто такой Смит?")).toBeTruthy();

  const active = await within(column()).findByRole("link", { name: "Про Смита" });
  expect(active.getAttribute("aria-current")).toBe("page");
  expect(active.getAttribute("href")).toBe("/chat/c1");
  const untitled = within(column()).getByRole("link", { name: "Без названия" });
  expect(untitled.getAttribute("aria-current")).toBeNull();
  expect(within(column()).getByRole("link", { name: /Новый диалог/ }).getAttribute("href")).toBe("/chat");

  expect(screen.getByRole("heading", { level: 1, name: "Про Смита" })).toBeTruthy();
  expect(screen.getByText("режим: Граф")).toBeTruthy();
  const metrics = screen.getByText("1,5 с · 100 + 20 = 120 токенов");
  expect(metrics.getAttribute("title")).toBe("Время ответа и токены LLM (вход + выход)");
  // источники ответа — под ним
  expect(screen.getByRole("button", { name: /smith\.pdf/ })).toBeTruthy();
});

test("клик по [1] открывает фрагмент-источник в панели", async () => {
  const router = await renderAt("/chat/c1", fakeApi(routes()));
  await screen.findByText("Кто такой Смит?");
  const cite = document.querySelector<HTMLAnchorElement>("a.cite")!;
  expect(cite.textContent).toBe("1");
  await click(cite);
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ panel: `chunk:${CHUNK_ID}` }));
  expect(router.state.location.pathname).toBe("/chat/c1");
});

test("клик по [n] источника wiki — переход на страницу wiki", async () => {
  const wiki: Source = { n: 1, kind: "wiki", ref: "e42#Раздел", path: "adam-smith.md", page: null, snippet: "", cited: true };
  const history = [msg({ id: "m2", role: "assistant", content: "См. [1].", sources: [wiki] })];
  const router = await renderAt("/chat/c1", fakeApi(routes({}, history)));
  await screen.findByText(/См\./);
  await click(document.querySelector<HTMLAnchorElement>("a.cite")!);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/wiki/page/e42"));
});

test("ровно 200 сообщений — предупреждение о последних 200", async () => {
  const many = Array.from({ length: 200 }, (_, i) =>
    msg({ id: `m${i}`, role: i % 2 ? "assistant" : "user", content: `Сообщение ${i}` }),
  );
  await renderAt("/chat/c1", fakeApi(routes({}, many)));
  expect(await screen.findByText("Показаны последние 200 сообщений диалога.")).toBeTruthy();
});

test("199 сообщений — без предупреждения", async () => {
  const many = Array.from({ length: 199 }, (_, i) => msg({ id: `m${i}`, content: `Сообщение ${i}` }));
  await renderAt("/chat/c1", fakeApi(routes({}, many)));
  await screen.findByText("Сообщение 198");
  expect(screen.queryByText("Показаны последние 200 сообщений диалога.")).toBeNull();
});

test("пустой диалог — «О чём спросить?» и пояснение", async () => {
  await renderAt("/chat/c1", fakeApi(routes({}, [])));
  expect(await screen.findByRole("heading", { name: "О чём спросить?" })).toBeTruthy();
  expect(
    screen.getByText(
      "Ответ строится по вашим документам. Номера в тексте ведут к источникам — нажмите, чтобы прочитать фрагмент целиком.",
    ),
  ).toBeTruthy();
});

test("/chat без диалога — пустой экран, история не запрашивается, шапки нет", async () => {
  const api = fakeApi(routes());
  await renderAt("/chat", api);
  expect(await screen.findByRole("heading", { name: "О чём спросить?" })).toBeTruthy();
  await within(column()).findByRole("link", { name: "Про Смита" });
  expect(api.requests().some((r) => r.includes("/messages"))).toBe(false);
  expect(screen.queryByRole("button", { name: "Переименовать" })).toBeNull();
});

test("пустой список диалогов — «Диалогов пока нет.»", async () => {
  await renderAt("/chat", fakeApi(routes({ "GET /api/conversations?limit=200": [] })));
  expect(await within(column()).findByText("Диалогов пока нет.")).toBeTruthy();
});

test("ошибка загрузки истории — текст detail", async () => {
  const api = fakeApi(
    routes({ [MESSAGES("c1")]: () => Response.json({ detail: "Диалог не найден" }, { status: 404 }) }),
  );
  await renderAt("/chat/c1", api);
  expect((await screen.findByRole("alert")).textContent).toContain("Диалог не найден");
});

test("переименование: PATCH {title} и перезапрос списка", async () => {
  let title = "Про Смита";
  const api = fakeApi(
    routes({
      "GET /api/conversations?limit=200": () => Response.json([conv("c1", title), conv("c2", "")]),
      "PATCH /api/conversations/c1": (body) => {
        title = (body as { title: string }).title;
        return Response.json(conv("c1", title));
      },
    }),
  );
  await renderAt("/chat/c1", api);
  await click(await screen.findByRole("button", { name: "Переименовать" }));
  const input = screen.getByRole("textbox", { name: "Название диалога" }) as HTMLInputElement;
  expect(input.value).toBe("Про Смита");
  expect(input.maxLength).toBe(200);
  fireEvent.change(input, { target: { value: "Новое" } });
  await click(screen.getByRole("button", { name: "Сохранить" }));
  expect(api.bodies("PATCH", "/api/conversations/c1")).toEqual([{ title: "Новое" }]);
  expect(await screen.findByRole("heading", { level: 1, name: "Новое" })).toBeTruthy();
  expect(count(api, "GET /api/conversations?limit=200")).toBe(2);
  expect(screen.queryByRole("textbox", { name: "Название диалога" })).toBeNull();
});

test("переименование: «Отмена» закрывает форму без запроса", async () => {
  const api = fakeApi(routes());
  await renderAt("/chat/c1", api);
  await click(await screen.findByRole("button", { name: "Переименовать" }));
  await click(screen.getByRole("button", { name: "Отмена" }));
  expect(screen.queryByRole("textbox", { name: "Название диалога" })).toBeNull();
  expect(api.requests().some((r) => r.startsWith("PATCH"))).toBe(false);
});

test("удаление: два клика, DELETE, переход на /chat и перезапрос списка", async () => {
  let deleted = false;
  const api = fakeApi(
    routes({
      "GET /api/conversations?limit=200": () =>
        Response.json(deleted ? [conv("c2", "")] : [conv("c1", "Про Смита"), conv("c2", "")]),
      "DELETE /api/conversations/c1": () => {
        deleted = true;
        return Response.json({ status: "ok" });
      },
    }),
  );
  const router = await renderAt("/chat/c1", api);
  await click(await screen.findByRole("button", { name: "Удалить" }));
  expect(api.requests().some((r) => r.startsWith("DELETE"))).toBe(false);
  await click(screen.getByRole("button", { name: "Удалить диалог" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
  expect(api.requests()).toContain("DELETE /api/conversations/c1");
  await vi.waitFor(() => expect(within(column()).queryByRole("link", { name: "Про Смита" })).toBeNull());
  expect(count(api, "GET /api/conversations?limit=200")).toBe(2);
});

test("композер: поле вопроса и выбор режима на странице", async () => {
  await renderAt("/chat", fakeApi(routes()));
  const box = (await screen.findByRole("textbox", { name: "Вопрос" })) as HTMLTextAreaElement;
  expect(box.maxLength).toBe(16000);
  expect(box.placeholder).toBe("Спросите о своих документах…");
  expect(screen.getByRole("radiogroup", { name: "Где искать ответ" })).toBeTruthy();
});

describe("автопрокрутка к концу истории", () => {
  async function renderWithClient(reduced: boolean) {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: reduced && query.includes("reduce"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
    }));
    const scrollTo = vi.fn();
    HTMLElement.prototype.scrollTo = scrollTo;
    vi.stubGlobal("fetch", fakeApi(routes()));
    const client = createQueryClient();
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/chat/c1"] }) });
    await router.load();
    render(<App router={router} client={client} />);
    await screen.findByText("Кто такой Смит?");
    await vi.waitFor(() => expect(client.isFetching()).toBe(0));
    return { client, scrollTo };
  }

  const append = (client: ReturnType<typeof createQueryClient>) =>
    act(async () => {
      client.setQueryData(["messages", "c1"], [...HISTORY, msg({ id: "m3", content: "Ещё вопрос" })]);
      await new Promise((r) => setTimeout(r, 0)); // Query оповещает подписчиков в следующем такте
    });

  afterEach(() => {
    delete (HTMLElement.prototype as { scrollTo?: unknown }).scrollTo;
  });

  test("открытие — сразу, новое сообщение — плавно", async () => {
    const { client, scrollTo } = await renderWithClient(false);
    expect(scrollTo).not.toHaveBeenCalled();
    await append(client);
    expect(screen.getByText("Ещё вопрос")).toBeTruthy();
    expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: "smooth" }));
  });

  test("при reduced motion — без анимации", async () => {
    const { client, scrollTo } = await renderWithClient(true);
    await append(client);
    expect(screen.getByText("Ещё вопрос")).toBeTruthy();
    expect(scrollTo).not.toHaveBeenCalled();
  });
});
