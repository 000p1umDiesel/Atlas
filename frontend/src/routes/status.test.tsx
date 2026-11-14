import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { AnalysisPayload, Job, StatusPayload, UsageRow } from "../api/types";
import { QueryClient } from "@tanstack/react-query";
import { App } from "../App";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const NOW = Math.floor(Date.now() / 1000);

const usage = (over: Partial<UsageRow> = {}): UsageRow => ({
  model: "gpt-x",
  currency: "USD",
  external: true,
  calls: 12,
  tokens_in: 1000,
  tokens_out: 234,
  errors: 1,
  estimated_cost: "0.42",
  ...over,
});

const status = (over: Partial<StatusPayload> = {}): StatusPayload => ({
  queue: {},
  running: [],
  worker: { last_seen: NOW - 5, alive: true },
  llm_usage: { today: [], month: [] },
  ...over,
});

const job = (over: Partial<Job> = {}): Job => ({
  id: 7,
  kind: "analyze",
  status: "running",
  attempts: 1,
  available_at: NOW,
  error: null,
  progress: "PageRank",
  created_at: NOW,
  updated_at: NOW,
  doc_id: null,
  name: "анализ графа",
  stage: null,
  step_done: null,
  step_total: null,
  ...over,
});

const analysis = (over: Partial<AnalysisPayload> = {}): AnalysisPayload => ({
  revision: null,
  run_id: null,
  ready: false,
  completed_at: null,
  metadata: {},
  top: [],
  job: null,
  ...over,
});

const STATUS = "GET /api/status";
const ANALYSIS = "GET /api/graph/analysis";

function routes(extra: Parameters<typeof fakeApi>[0] = {}): Parameters<typeof fakeApi>[0] {
  return { "GET /api/auth/me": ME, [STATUS]: status(), [ANALYSIS]: analysis(), ...extra };
}

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: false, refetchOnWindowFocus: false } } })} />);
  return router;
}

async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
    await new Promise((r) => setTimeout(r, 0));
  });
}

const count = (api: FakeApi, request: string) => api.requests().filter((r) => r === request).length;
const section = (name: string) => screen.getByRole("heading", { level: 2, name }).closest("section")!;

beforeAll(() => {
  window.scrollTo = vi.fn();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("worker ни разу не запускался; очередь пуста", async () => {
  const api = fakeApi(routes({ [STATUS]: status({ worker: { last_seen: null, alive: false } }) }));
  await renderAt("/status", api);
  expect(await screen.findByText("Worker ещё ни разу не запускался")).toBeTruthy();
  expect(screen.getByText("Очередь пуста.")).toBeTruthy();
});

test("worker не отвечает; счётчики очереди и выполняемые задачи", async () => {
  const api = fakeApi(
    routes({
      [STATUS]: status({
        worker: { last_seen: NOW - 600, alive: false },
        queue: { pending: 2, running: 1 },
        running: [job({ id: 1, kind: "ingest", name: "a.pdf", progress: "embed 3/10" }), job({ id: 2, name: "b.pdf", progress: "" })],
      }),
    }),
  );
  await renderAt("/status", api);
  expect(await screen.findByText(/^Worker не отвечает: последний раз/)).toBeTruthy();
  const block = section("Обработка документов");
  expect(within(block).getByText(/ждёт:/).textContent).toContain("2");
  expect(within(block).getByText("a.pdf — embed 3/10")).toBeTruthy();
  expect(within(block).getByText("b.pdf — начинается")).toBeTruthy();
});

test("worker работает", async () => {
  await renderAt("/status", fakeApi(routes()));
  expect(await screen.findByText(/^Worker работает, отмечался/)).toBeTruthy();
});

test("расход LLM: «локально», «—» без оценки, пустой список", async () => {
  const api = fakeApi(
    routes({
      [STATUS]: status({
        llm_usage: {
          today: [usage(), usage({ model: "qwen", external: false, currency: null, estimated_cost: null, tokens_in: null, tokens_out: null })],
          month: [],
        },
      }),
    }),
  );
  await renderAt("/status", api);
  await screen.findByRole("heading", { level: 2, name: "Расход LLM" });
  const block = section("Расход LLM");
  const today = (await within(block).findByRole("heading", { level: 3, name: "Сегодня (UTC)" })).parentElement!;
  const rows = within(today).getAllByRole("row");
  expect(rows[0]!.textContent).toBe("МодельВызовыТокеныОшибкиОценка");
  expect(rows[1]!.textContent).toContain("gpt-x");
  expect(rows[1]!.textContent).toContain("0.42 USD");
  expect(rows[1]!.textContent).not.toContain("локально");
  expect(rows[2]!.textContent).toContain("qwen");
  expect(rows[2]!.textContent).toContain("локально");
  expect(rows[2]!.textContent).toContain("—");
  const month = within(block).getByRole("heading", { level: 3, name: "Этот месяц" }).parentElement!;
  expect(within(month).getByText("Вызовов не было.")).toBeTruthy();
});

test("ошибка после первой загрузки — над прежними данными", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let down = false;
  const api = fakeApi(routes({ [STATUS]: () => (down ? fail(500, "x") : Response.json(status({ queue: { done: 3 } }))) }));
  await renderAt("/status", api);
  await vi.waitFor(() => expect(screen.getByText(/готово:/)).toBeTruthy());
  down = true;
  await act(async () => {
    vi.advanceTimersByTime(5000);
  });
  await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toContain("x"), { timeout: 1000 });
  expect(screen.getByText(/готово:/)).toBeTruthy();
});

test("ошибка первой загрузки — только сообщение", async () => {
  await renderAt("/status", fakeApi(routes({ [STATUS]: () => fail(500, "x") })));
  expect(await screen.findByRole("alert", {}, { timeout: 1000 })).toBeTruthy();
  expect(screen.queryByRole("heading", { level: 2, name: "Расход LLM" })).toBeNull();
});

test("анализ: запуск, опрос раз в 5000 мс пока идёт, остановка при done", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let current: AnalysisPayload = analysis();
  const api = fakeApi(
    routes({
      [ANALYSIS]: () => Response.json(current),
      "POST /api/graph/analyze": () => {
        current = analysis({ job: job() });
        return Response.json(job(), { status: 202 });
      },
    }),
  );
  await renderAt("/status", api);
  await vi.waitFor(() => expect(screen.getByText("Анализ ещё не запускался.")).toBeTruthy());
  await act(async () => {
    vi.advanceTimersByTime(20_000);
  });
  expect(count(api, ANALYSIS)).toBe(1);

  await click(screen.getByRole("button", { name: "Запустить анализ графа" }));
  expect(api.requests()).toContain("POST /api/graph/analyze");
  await vi.waitFor(() => expect((screen.getByRole("button", { name: "Анализ идёт…" }) as HTMLButtonElement).disabled).toBe(true));
  expect(screen.getByText("Задача:").closest("p")!.textContent).toContain("PageRank");
  const before = count(api, ANALYSIS);

  current = analysis({
    ready: true,
    completed_at: "2026-11-14T10:00:00Z",
    metadata: { nodes: 10, relationships: 20, communities: 3 },
    top: [{ entity_id: "e1", name: "Смит", pagerank: 0.12345, community: "1" }],
    job: job({ status: "done" }),
  });
  await act(async () => {
    vi.advanceTimersByTime(5000);
  });
  await vi.waitFor(() => expect(screen.getByText(/сообществ\./)).toBeTruthy());
  expect(count(api, ANALYSIS)).toBeGreaterThan(before);
  // завершение задачи перечитывает и статус очереди
  await vi.waitFor(() => expect(count(api, STATUS)).toBeGreaterThan(1));
  const after = count(api, ANALYSIS);
  await act(async () => {
    vi.advanceTimersByTime(20_000);
  });
  expect(count(api, ANALYSIS)).toBe(after);
  expect(screen.getByRole("button", { name: "Запустить анализ графа" })).toBeTruthy();
  expect(screen.getByRole("button", { name: /Смит/ }).closest("li")!.textContent).toContain("0.123");
});

test("анализ: нет готового снимка после прошлого запуска; 429 при запуске", async () => {
  const api = fakeApi(
    routes({
      [ANALYSIS]: analysis({ metadata: { nodes: 1 } }),
      "POST /api/graph/analyze": () => fail(429, "Очередь обработки заполнена"),
    }),
  );
  await renderAt("/status", api);
  expect(await screen.findByText("Готового снимка нет: граф изменился после прошлого анализа.")).toBeTruthy();
  await click(screen.getByRole("button", { name: "Запустить анализ графа" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Очередь обработки заполнена");
});

test("анализ: упавшая задача без префикса AnalysisError", async () => {
  await renderAt("/status", fakeApi(routes({ [ANALYSIS]: analysis({ job: job({ status: "failed", error: "AnalysisError: нет GDS" }) }) })));
  const line = (await screen.findByText("Задача:")).closest("p")!.textContent!;
  expect(line).toContain("нет GDS");
  expect(line).not.toContain("AnalysisError");
});

test("самые связанные: клик открывает панель сущности", async () => {
  const api = fakeApi(
    routes({
      [ANALYSIS]: analysis({
        ready: true,
        completed_at: "2026-11-14T10:00:00Z",
        metadata: { nodes: 1, relationships: 2, communities: 3 },
        top: [{ entity_id: "e1", name: "Смит", pagerank: 0.5, community: null }],
      }),
    }),
  );
  const router = await renderAt("/status", api);
  await click(await screen.findByRole("button", { name: /Смит/ }));
  await vi.waitFor(() => expect(router.state.location.search).toMatchObject({ panel: "entity:e1" }));
});

test("проверки: 429, warn и сбой", async () => {
  let n = 0;
  const api = fakeApi(
    routes({
      "GET /api/status/checks": () =>
        ++n === 1
          ? fail(429, "Проверка уже выполняется")
          : Response.json([
              { name: "database", ok: true, detail: "ок", warn: false },
              { name: "llm", ok: true, detail: "медленно", warn: true },
              { name: "neo4j", ok: false, detail: "Недоступно", warn: false },
            ]),
    }),
  );
  await renderAt("/status", api);
  await screen.findByText(/Worker/);
  expect(count(api, "GET /api/status/checks")).toBe(0);
  await click(screen.getByRole("button", { name: "Проверить сервисы" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Проверка уже выполняется");
  await click(screen.getByRole("button", { name: "Проверить сервисы" }));
  const block = section("Сервисы");
  await vi.waitFor(() => expect(within(block).getByText("внимание: медленно")).toBeTruthy());
  expect(within(block).getByText("PostgreSQL")).toBeTruthy();
  expect(within(block).getByText("LLM")).toBeTruthy();
  expect(within(block).getByText("Neo4j (граф)").closest("li")!.getAttribute("data-state")).toBe("fail");
});

test.each(["/chat", "/documents", "/wiki", "/status"])("%s — настоящий экран, не заглушка", async (path) => {
  await renderAt(path, fakeApi(routes({ "GET /api/conversations": [], "GET /api/documents?limit=200&offset=0": [], "GET /api/jobs?limit=30": [], "GET /api/wiki": [] })));
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20));
  });
  expect(screen.queryByText("Появится на следующем этапе")).toBeNull();
});
