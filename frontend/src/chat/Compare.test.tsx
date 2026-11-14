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
