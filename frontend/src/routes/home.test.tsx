import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { generateFixtures } from "../fixtures/generate";
import { createAppRouter } from "../router";
import { AnalysisCard } from "./home";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
vi.mock("../graph/Graph3D", async () => ({ default: (await import("../test/fakeGraph")).FakeGraph3D }));

async function renderHome() {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  await screen.findByRole("list", { name: "Сущности по типам" });
  return router;
}

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});

beforeEach(() => vi.stubEnv("VITE_FIXTURES", "1"));
afterEach(() => vi.unstubAllEnvs());

test("плитка «Сущности» показывает 300", async () => {
  await renderHome();
  const tile = screen.getByRole("group", { name: "Сущности" });
  expect(within(tile).getByText("300")).toBeTruthy();
  for (const name of ["Документы", "Связи", "Страницы wiki"]) {
    expect(screen.getByRole("group", { name })).toBeTruthy();
  }
});

test("числа в плитках — в русской записи", async () => {
  const { stats } = generateFixtures();
  await renderHome();
  const tile = screen.getByRole("group", { name: "Документы" });
  expect(tile.textContent).toContain(`${stats.chunks.toLocaleString("ru-RU")} фрагментов`);
});

test("полос по типам — 18, строка «Человек» с числом из фикстуры", async () => {
  const { types } = generateFixtures();
  await renderHome();
  const bars = screen.getByRole("list", { name: "Сущности по типам" });
  expect(within(bars).getAllByRole("listitem")).toHaveLength(18);
  expect(within(bars).queryByText("Person")).toBeNull();
  const person = within(bars).getByRole("link", { name: /^Человек/ });
  expect(person.textContent).toContain(String(types.find((t) => t.type === "Person")!.count));
  expect(person.getAttribute("href")).toBe("/graph?types=Person");
});

test("«Что это за типы?» раскрывает справочник: название и определение каждого типа", async () => {
  const { types } = generateFixtures();
  await renderHome();
  const toggle = screen.getByRole("button", { name: "Что это за типы?" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("region", { name: "Типы сущностей" })).toBeNull();
  await act(async () => fireEvent.click(toggle));
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const glossary = screen.getByRole("region", { name: "Типы сущностей" });
  expect(toggle.getAttribute("aria-controls")).toBe(glossary.id);
  const terms = within(glossary).getAllByRole("term");
  expect(terms.map((t) => t.textContent)).toEqual(types.map((t) => t.label));
  const definitions = within(glossary).getAllByRole("definition");
  expect(definitions.map((d) => d.textContent)).toEqual(types.map((t) => t.description));
  await act(async () => fireEvent.click(toggle));
  await vi.waitFor(() => expect(screen.queryByRole("region", { name: "Типы сущностей" })).toBeNull());
});

test("в подписи к главной и в списке важных сущностей — русские названия типов", async () => {
  const { types } = generateFixtures();
  await renderHome();
  const lead = [...types].sort((a, b) => b.count - a.count)[0]!;
  expect(screen.getByText(new RegExp(`сущностей типа «${lead.label}»`))).toBeTruthy();
  const list = screen.getByRole("list", { name: "Самые важные сущности" });
  for (const t of types) expect(within(list).queryByText(new RegExp(`^${t.type},`))).toBeNull();
});

test("самые важные сущности ведут на граф с подсветкой", async () => {
  const { graph } = generateFixtures();
  const top = [...graph.nodes].sort((a, b) => (b.pagerank ?? 0) - (a.pagerank ?? 0))[0]!;
  const router = await renderHome();
  const list = screen.getByRole("list", { name: "Самые важные сущности" });
  expect(within(list).getAllByRole("listitem")).toHaveLength(8);
  const escaped = top.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const link = within(list).getByRole("link", { name: new RegExp(`^${escaped}`) });
  expect(link.getAttribute("href")).toBe(`/graph?${new URLSearchParams({ highlight: top.name })}`);
  await act(async () => {
    fireEvent.click(link);
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/graph"));
  expect(router.state.location.search).toMatchObject({ highlight: top.name });
});

test("последние документы — 5, анализ графа готов", async () => {
  await renderHome();
  const docs = screen.getByRole("list", { name: "Последние документы" });
  expect(within(docs).getAllByRole("listitem")).toHaveLength(5);
  expect(screen.getByRole("heading", { name: "Анализ графа" })).toBeTruthy();
  expect(screen.getByText("Готов")).toBeTruthy();
});

test("загрузка в очереди (status queued, updated_at: null) подписана «в очереди»", async () => {
  await renderHome();
  const docs = screen.getByRole("list", { name: "Последние документы" });
  const first = within(docs).getAllByRole("listitem")[0]!.textContent ?? "";
  expect(first).toContain("в очереди");
  expect(first).toContain("В очереди");
  expect(first).not.toContain("queued");
});

test("без сессии — переход на вход, 401 не повторяется", async () => {
  vi.stubEnv("VITE_FIXTURES", "");
  const fetchMock = vi.fn(async () => Response.json({ detail: "Требуется вход" }, { status: 401 }));
  vi.stubGlobal("fetch", fetchMock);
  try {
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
    await router.load();
    render(<App router={router} client={createQueryClient()} />);
    await vi.waitFor(() => expect(router.state.location.href).toBe("/login?next=%2F"));
    expect(await screen.findByLabelText("Имя пользователя")).toBeTruthy();
    // 401 не повторяется, сводка и последние документы — один запрос
    const urls = fetchMock.mock.calls.map((call) => String((call as unknown[])[0]));
    expect(urls.filter((url) => url === "/api/stats")).toHaveLength(1);
  } finally {
    vi.unstubAllGlobals();
  }
});

/** Карточка отдельно от страницы: ей нужен только роутер для ссылок. */
async function renderCard(completedAt: string | null) {
  const root = createRootRoute({
    component: () => <AnalysisCard ready={false} completedAt={completedAt} communities={[]} />,
  });
  const router = createRouter({
    routeTree: root.addChildren([createRoute({ getParentRoute: () => root, path: "/graph" })]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  await router.load();
  render(<RouterProvider router={router} />);
}

test("анализ устарел: дата последнего запуска, не «Не запускался»", async () => {
  await renderCard("2026-11-12T21:40:00.000Z");
  expect(screen.getByText("Устарел — граф изменился после анализа")).toBeTruthy();
  expect(screen.getByText(/Последний запуск/)).toBeTruthy();
  expect(screen.queryByText("Не запускался")).toBeNull();
  expect(screen.queryByText(/ещё не посчитаны/)).toBeNull();
});

test("анализ не запускался: completed_at пуст", async () => {
  await renderCard(null);
  expect(screen.getByText("Не запускался")).toBeTruthy();
  expect(screen.getByText(/ещё не посчитаны/)).toBeTruthy();
  expect(screen.queryByText(/Последний запуск/)).toBeNull();
});

test("«Пересчитать» запускает анализ и перечитывает сводку", async () => {
  vi.stubEnv("VITE_FIXTURES", "");
  const { stats } = generateFixtures();
  let current: unknown = { revision: null, run_id: null, ready: false, completed_at: null, metadata: {}, top: [], job: null };
  const job = { id: 3, kind: "analyze", status: "pending", attempts: 0, available_at: 0, error: null, progress: "", created_at: 0, updated_at: 0, doc_id: null, name: "анализ графа" };
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.split("?")[0];
    if (init?.method === "POST" && path === "/api/graph/analyze") {
      current = { ...(current as object), job };
      return Response.json(job, { status: 202 });
    }
    if (path === "/api/stats") return Response.json(stats);
    if (path === "/api/graph/analysis") return Response.json(current);
    if (path === "/api/graph/overview") return Response.json({ nodes: [], relations: [] });
    if (path === "/api/entities/types") return Response.json([]);
    return Response.json({ detail: "Не найдено" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  try {
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
    await router.load();
    render(<App router={router} client={createQueryClient()} />);
    const button = await screen.findByRole("button", { name: "Пересчитать" });
    const statsBefore = fetchMock.mock.calls.filter((c) => c[0] === "/api/stats").length;
    await act(async () => {
      fireEvent.click(button);
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(fetchMock.mock.calls.some((c) => c[1]?.method === "POST" && c[0] === "/api/graph/analyze")).toBe(true);
    await vi.waitFor(() => expect((screen.getByRole("button", { name: "Анализ идёт…" }) as HTMLButtonElement).disabled).toBe(true));
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter((c) => c[0] === "/api/stats").length).toBeGreaterThan(statsBefore));
  } finally {
    vi.unstubAllGlobals();
  }
});

test("после завершения анализа карточка главной перечитывает сводку", async () => {
  vi.stubEnv("VITE_FIXTURES", "");
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const { stats } = generateFixtures();
  const job = { id: 3, kind: "analyze", status: "running", attempts: 1, available_at: 0, error: null, progress: "", created_at: 0, updated_at: 0, doc_id: null, name: "анализ графа" };
  let analysis: unknown = { revision: null, run_id: null, ready: false, completed_at: null, metadata: {}, top: [], job };
  let ready = false;
  const fetchMock = vi.fn(async (url: string) => {
    const path = url.split("?")[0];
    if (path === "/api/stats") return Response.json({ ...stats, analysis: { ...stats.analysis, ready, completed_at: ready ? "2026-11-14T10:00:00Z" : null } });
    if (path === "/api/graph/analysis") return Response.json(analysis);
    if (path === "/api/graph/overview") return Response.json({ nodes: [], relations: [] });
    if (path === "/api/entities/types") return Response.json([]);
    return Response.json({ detail: "Не найдено" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  try {
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
    await router.load();
    render(<App router={router} client={createQueryClient()} />);
    await screen.findByText("Не запускался");
    ready = true;
    analysis = { ...(analysis as object), ready: true, job: { ...job, status: "done" } };
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    await vi.waitFor(() => expect(screen.getByText("Готов")).toBeTruthy());
  } finally {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  }
});
