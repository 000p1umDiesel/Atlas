import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import type { StatusPayload, UsageRow } from "../api/types";
import { fail, fakeApi } from "../test/fakeApi";
import { addCosts, WorkCard } from "./WorkCard";

const NOW = Math.floor(Date.now() / 1000);

const row = (over: Partial<UsageRow> = {}): UsageRow => ({
  model: "m",
  currency: "RUB",
  external: true,
  calls: 1,
  tokens_in: 10,
  tokens_out: 5,
  errors: 0,
  estimated_cost: "0.10",
  ...over,
});

const status = (over: Partial<StatusPayload> = {}): StatusPayload => ({
  queue: {},
  running: [],
  worker: { last_seen: NOW - 5, alive: true },
  llm_usage: { today: [], month: [] },
  ...over,
});

async function renderCard(reply: Response | object) {
  vi.stubGlobal("fetch", fakeApi({ "GET /api/status": reply instanceof Response ? () => reply : reply }));
  const root = createRootRoute({ component: WorkCard });
  const router = createRouter({
    routeTree: root.addChildren([createRoute({ getParentRoute: () => root, path: "/status", component: () => null })]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  await router.load();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("worker ни разу не запускался", async () => {
  await renderCard(status({ worker: { alive: false, last_seen: null } }));
  expect(await screen.findByText("Worker ещё ни разу не запускался")).toBeTruthy();
});

test("worker работает и очередь с числами", async () => {
  await renderCard(status({ queue: { pending: 2, failed: 1 } }));
  expect(await screen.findByText(/Worker работает/)).toBeTruthy();
  expect(screen.getByText("2 в очереди")).toBeTruthy();
  expect(screen.getByText("1 с ошибкой")).toBeTruthy();
  expect(screen.getByText("0 в работе")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Подробнее" }).getAttribute("href")).toBe("/status");
});

test("worker не отвечает: последний раз", async () => {
  await renderCard(status({ worker: { alive: false, last_seen: NOW - 7200 } }));
  expect(await screen.findByText(/последний раз/)).toBeTruthy();
});

test("стоимость по валютам суммируется без ошибок float", async () => {
  await renderCard(
    status({ llm_usage: { today: [], month: [row({ estimated_cost: "0.10" }), row({ estimated_cost: "0.25", calls: 4 })] } }),
  );
  expect(await screen.findByText("0.35 RUB")).toBeTruthy();
  expect(screen.getByText("5 вызовов")).toBeTruthy();
});

test("строка без тарифа помечена, токены null пропускаются", async () => {
  await renderCard(
    status({ llm_usage: { today: [], month: [row({ estimated_cost: null, tokens_in: null, tokens_out: null, currency: null })] } }),
  );
  expect(await screen.findByText(/без тарифа/)).toBeTruthy();
});

test("addCosts: разное число знаков, по валютам", () => {
  expect(addCosts([row({ estimated_cost: "1.5" }), row({ estimated_cost: "0.125" }), row({ currency: "USD", estimated_cost: "2" })])).toEqual([
    { currency: "RUB", total: "1.625" },
    { currency: "USD", total: "2" },
  ]);
});

test("ошибка статуса — карточка недоступна", async () => {
  await renderCard(fail(500, "boom"));
  expect(await screen.findByText("Сводка обработки пока недоступна")).toBeTruthy();
});
