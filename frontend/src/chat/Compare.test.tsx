import { act, fireEvent, screen, within } from "@testing-library/react";
import { useState } from "react";
import { deferred, fail, fakeApi } from "../test/fakeApi";
import { renderWithRouter } from "../test/router";
import { Compare } from "./Compare";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function answer(mode: string, over: object = {}) {
  return {
    question: "Вопрос",
    mode,
    text: `Ответ ${mode}`,
    sources: [],
    alt_queries: [],
    latency_ms: 1500,
    tokens_in: 100,
    tokens_out: 20,
    ...over,
  };
}

const modeOf = (api: ReturnType<typeof fakeApi>) =>
  (api.bodies("POST", "/api/ask") as { mode: string }[]).map((b) => b.mode);
const card = (title: string) =>
  screen
    .getByRole("heading", { name: title })
    .closest("section") as HTMLElement;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

test("запросы строго по очереди; до своего запроса карточки «В очереди»", async () => {
  const first = deferred<Response>();
  const api = fakeApi({
    "POST /api/ask": (body) => {
      const mode = (body as { mode: string }).mode;
      return mode === "rag" ? first.promise : Response.json(answer(mode));
    },
  });
  vi.stubGlobal("fetch", api);
  await renderWithRouter(<Compare question="Вопрос" onClose={vi.fn()} />);
  await act(tick);
  expect(modeOf(api)).toEqual(["rag"]);
  expect(within(card("Фрагменты")).getByText("Отвечаю…")).toBeTruthy();
  for (const t of ["Wiki", "Граф", "Всё вместе"])
    expect(within(card(t)).getByText("В очереди")).toBeTruthy();
  await act(async () => {
    first.resolve(Response.json(answer("rag")));
    await tick();
    await tick();
  });
  await vi.waitFor(() =>
    expect(modeOf(api)).toEqual(["rag", "wiki", "graph", "all"]),
  );
  expect(await screen.findByText("Ответ rag")).toBeTruthy();
  expect(
    within(card("Фрагменты")).getByText("1,5 с · 100 + 20 токенов"),
  ).toBeTruthy();
});

test("503 у wiki — ошибка у карточки, graph и all всё равно выполнены", async () => {
  const api = fakeApi({
    "POST /api/ask": (body) => {
      const mode = (body as { mode: string }).mode;
      return mode === "wiki"
        ? new Response(null, { status: 503 })
        : Response.json(answer(mode));
    },
  });
  vi.stubGlobal("fetch", api);
  await renderWithRouter(<Compare question="Вопрос" onClose={vi.fn()} />);
  await vi.waitFor(() => expect(modeOf(api)).toHaveLength(4));
  expect(
    within(card("Wiki")).getByText("Сервис временно недоступен"),
  ).toBeTruthy();
  expect(await screen.findByText("Ответ all")).toBeTruthy();
});

test("detail ошибки показывается как есть", async () => {
  const api = fakeApi({
    "POST /api/ask": () => fail(429, "Слишком много запросов"),
  });
  vi.stubGlobal("fetch", api);
  await renderWithRouter(<Compare question="Вопрос" onClose={vi.fn()} />);
  await vi.waitFor(() => expect(modeOf(api)).toHaveLength(4));
  expect(within(card("Граф")).getByText("Слишком много запросов")).toBeTruthy();
});

test("«Скрыть» после первого ответа: остальные запросы не отправляются", async () => {
  const second = deferred<Response>();
  const onClose = vi.fn();
  function Host() {
    const [shown, setShown] = useState(true);
    return shown ? (
      <Compare
        question="Вопрос"
        onClose={() => {
          onClose();
          setShown(false);
        }}
      />
    ) : null;
  }
  const api = fakeApi({
    "POST /api/ask": (body) => {
      const mode = (body as { mode: string }).mode;
      return mode === "rag" ? Response.json(answer(mode)) : second.promise;
    },
  });
  vi.stubGlobal("fetch", api);
  await renderWithRouter(<Host />);
  await vi.waitFor(() => expect(modeOf(api)).toEqual(["rag", "wiki"]));
  fireEvent.click(screen.getByRole("button", { name: "Скрыть" }));
  expect(onClose).toHaveBeenCalled();
  await act(async () => {
    second.resolve(Response.json(answer("wiki")));
    await tick();
  });
  // Родитель убирает компонент: цикл прекращён.
  expect(modeOf(api)).toEqual(["rag", "wiki"]);
});

test("размонтирование прерывает идущий запрос и цикл", async () => {
  const pending = deferred<Response>();
  const api = fakeApi({ "POST /api/ask": () => pending.promise });
  vi.stubGlobal("fetch", api);
  const { unmount } = await renderWithRouter(
    <Compare question="Вопрос" onClose={vi.fn()} />,
  );
  await vi.waitFor(() => expect(modeOf(api)).toEqual(["rag"]));
  const signal = api.mock.calls[0]![1]!.signal!;
  unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => {
    pending.resolve(Response.json(answer("rag")));
    await tick();
  });
  expect(modeOf(api)).toEqual(["rag"]);
});

test("[n] открывает источник своего режима", async () => {
  const source = {
    n: 1,
    kind: "chunk",
    ref: "0123456789abcdef:00003",
    path: "a.pdf",
    page: 1,
    snippet: "",
    cited: true,
  };
  const api = fakeApi({
    "POST /api/ask": (body) => {
      const mode = (body as { mode: string }).mode;
      return Response.json(
        answer(mode, {
          text: "Ответ [1].",
          sources: mode === "rag" ? [source] : [],
        }),
      );
    },
  });
  vi.stubGlobal("fetch", api);
  const { router } = await renderWithRouter(
    <Compare question="Вопрос" onClose={vi.fn()} />,
  );
  await vi.waitFor(() => expect(modeOf(api)).toHaveLength(4));
  const links = await screen.findAllByRole("link", { name: "1" });
  fireEvent.click(links[0]!);
  await vi.waitFor(() =>
    expect(JSON.stringify(router.state.location)).toContain(
      "0123456789abcdef:00003",
    ),
  );
});

test("в чате: сравнение не создаёт диалог и сообщения", async () => {
  const { createMemoryHistory } = await import("@tanstack/react-router");
  const { render } = await import("@testing-library/react");
  const { App, createQueryClient } = await import("../App");
  const { createAppRouter } = await import("../router");
  window.scrollTo = vi.fn();
  const api = fakeApi({
    "GET /api/auth/me": { id: "u1", username: "owner" },
    "GET /api/conversations?limit=200": [],
    "POST /api/ask": (body) => Response.json(answer((body as { mode: string }).mode)),
  });
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/chat"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Сравнить режимы" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "Вопрос" }), { target: { value: "Вопрос" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Сравнить" }));
    await tick();
  });
  await vi.waitFor(() => expect(modeOf(api)).toHaveLength(4));
  expect(await screen.findByText("Сравнение режимов")).toBeTruthy();
  expect(api.requests().filter((r) => r.startsWith("POST") && !r.includes("/api/ask"))).toEqual([]);
});

test("401: оставшиеся режимы не запрашиваются", async () => {
  const api = fakeApi({ "POST /api/ask": () => fail(401, "Нужен вход") });
  vi.stubGlobal("fetch", api);
  await renderWithRouter(<Compare question="Вопрос" onClose={vi.fn()} />);
  await act(tick);
  await act(tick);
  expect(modeOf(api)).toEqual(["rag"]);
});

test("новый вопрос (другой key) прерывает предыдущий цикл", async () => {
  const pending = deferred<Response>();
  const api = fakeApi({ "POST /api/ask": () => pending.promise });
  vi.stubGlobal("fetch", api);
  function Host() {
    const [n, setN] = useState(1);
    return (
      <>
        <button onClick={() => setN(2)}>новый</button>
        <Compare key={n} question={`Вопрос ${n}`} onClose={vi.fn()} />
      </>
    );
  }
  await renderWithRouter(<Host />);
  await vi.waitFor(() => expect(api.mock.calls).toHaveLength(1));
  const signal = api.mock.calls[0]![1]!.signal!;
  fireEvent.click(screen.getByRole("button", { name: "новый" }));
  expect(signal.aborted).toBe(true);
  await vi.waitFor(() => expect(api.mock.calls).toHaveLength(2));
  expect(api.bodies("POST", "/api/ask")[1]).toEqual({ question: "Вопрос 2", mode: "rag" });
});

function matchMediaStub(reduced: boolean) {
  vi.stubGlobal("matchMedia", () => ({ matches: reduced }));
}

test.each([
  [true, "auto"],
  [false, "smooth"],
])("прокрутка к сравнению: reduced=%s → behavior %s", async (reduced, behavior) => {
  matchMediaStub(reduced);
  const api = fakeApi({ "POST /api/ask": () => new Promise<Response>(() => {}) });
  vi.stubGlobal("fetch", api);
  await renderWithRouter(<Compare question="Вопрос" onClose={vi.fn()} />);
  expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior, block: "nearest" });
});

test("переход в другой диалог закрывает сравнение и прерывает запрос", async () => {
  const { createMemoryHistory } = await import("@tanstack/react-router");
  const { render } = await import("@testing-library/react");
  const { App, createQueryClient } = await import("../App");
  const { createAppRouter } = await import("../router");
  window.scrollTo = vi.fn();
  const pending = deferred<Response>();
  const conv = (id: string) => ({ id, user_id: "u1", title: `Диалог ${id}`, created_at: "2026-11-14T10:00:00Z" });
  const api = fakeApi({
    "GET /api/auth/me": { id: "u1", username: "owner" },
    "GET /api/conversations?limit=200": [conv("c1"), conv("c2")],
    "GET /api/conversations/c1/messages?limit=200&newest=true": [],
    "GET /api/conversations/c2/messages?limit=200&newest=true": [],
    "POST /api/ask": () => pending.promise,
  });
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/chat/c1"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Сравнить режимы" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "Вопрос" }), { target: { value: "Вопрос" } });
  fireEvent.click(screen.getByRole("button", { name: "Сравнить" }));
  await screen.findByText("Сравнение режимов");
  await vi.waitFor(() => expect(api.mock.calls.some(([u]) => u === "/api/ask")).toBe(true));
  const call = api.mock.calls.find(([u]) => u === "/api/ask")!;
  await act(async () => {
    await router.navigate({ to: "/chat/$id", params: { id: "c2" } });
  });
  expect(call[1]!.signal!.aborted).toBe(true);
  expect(screen.queryByText("Сравнение режимов")).toBeNull();
});
