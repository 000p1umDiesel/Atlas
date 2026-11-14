import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { DocumentListItem, Job } from "../api/types";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const NOW = Math.floor(Date.now() / 1000);

const doc = (id: string, name: string, status = "done"): DocumentListItem => ({
  id,
  name,
  status,
  uploaded: true,
  url: `/api/documents/${id}/content`,
});

const job = (over: Partial<Job>): Job => ({
  id: 1,
  kind: "ingest",
  status: "done",
  attempts: 1,
  available_at: NOW,
  error: null,
  progress: "",
  created_at: NOW - 600,
  updated_at: NOW - 300,
  doc_id: null,
  name: "a.pdf",
  ...over,
});

const DOCS = "GET /api/documents?limit=200&offset=0";
const JOBS = "GET /api/jobs?limit=30";

type Routes = Parameters<typeof fakeApi>[0];

function routes(extra: Routes = {}): Routes {
  return {
    "GET /api/auth/me": ME,
    [DOCS]: [doc("d1", "Смит.pdf"), doc("d2", "Рикардо.docx", "failed")],
    [JOBS]: [],
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

async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
    await new Promise((r) => setTimeout(r, 0));
  });
}

const count = (api: FakeApi, request: string) => api.requests().filter((r) => r === request).length;
const section = (name: string) => screen.getByRole("heading", { level: 2, name: new RegExp(`^${name}`) }).closest("section")!;

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("список документов: ссылки на карточки, статусы, удаление; очередь пуста", async () => {
  await renderAt("/documents", fakeApi(routes()));
  expect(await screen.findByRole("heading", { level: 1, name: "Документы" })).toBeTruthy();
  const docs = section("В базе");
  const link = await within(docs).findByRole("link", { name: "Смит.pdf" });
  expect(link.getAttribute("href")).toBe("/documents/d1");
  const headers = within(docs).getAllByRole("columnheader").map((th) => th.textContent);
  expect(headers.slice(0, 2)).toEqual(["Название", "Статус"]);
  expect(within(docs).getByText("готово")).toBeTruthy();
  expect(within(docs).getByText("ошибка")).toBeTruthy();
  expect(within(docs).getAllByRole("button", { name: "Удалить" })).toHaveLength(2);
  expect(within(docs).queryByRole("button", { name: "Показать ещё" })).toBeNull();
  expect(within(section("Очередь")).getByText("Очередь пуста.")).toBeTruthy();
  expect(within(section("Очередь")).queryByText("обновляется")).toBeNull();
});

test("пустая база — пояснение", async () => {
  await renderAt("/documents", fakeApi(routes({ [DOCS]: [] })));
  expect(
    await screen.findByText(
      "Здесь пока пусто. Загрузите первый документ — после обработки по нему можно будет задавать вопросы.",
    ),
  ).toBeTruthy();
});

test("удаление в два клика — DELETE и перезапрос списка", async () => {
  const api = fakeApi(routes({ "DELETE /api/documents/d1": job({ id: 9, kind: "remove", status: "pending" }) }));
  await renderAt("/documents", api);
  const docs = section("В базе");
  await within(docs).findByRole("link", { name: "Смит.pdf" });
  const row = within(docs).getByRole("link", { name: "Смит.pdf" }).closest("tr")!;
  await click(within(row).getByRole("button", { name: "Удалить" }));
  expect(api.requests()).not.toContain("DELETE /api/documents/d1");
  await click(within(row).getByRole("button", { name: "Удалить из базы?" }));
  expect(api.requests()).toContain("DELETE /api/documents/d1");
  await vi.waitFor(() => expect(count(api, DOCS)).toBe(2));
  expect(count(api, JOBS)).toBe(2);
});

test("ошибка удаления — текст detail", async () => {
  const api = fakeApi(
    routes({ "DELETE /api/documents/d1": () => Response.json({ detail: "Очередь обработки заполнена" }, { status: 429 }) }),
  );
  await renderAt("/documents", api);
  const row = (await screen.findByRole("link", { name: "Смит.pdf" })).closest("tr")!;
  await click(within(row).getByRole("button", { name: "Удалить" }));
  await click(within(row).getByRole("button", { name: "Удалить из базы?" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Очередь обработки заполнена");
});

test("таблица очереди: подписи, ход, ссылка на документ, «Повторить» у ошибки", async () => {
  const jobs = [
    job({ id: 1, kind: "ingest", status: "done", doc_id: "d1", name: "Смит.pdf" }),
    job({ id: 2, kind: "remove", status: "failed", error: "QdrantError", name: "old.pdf" }),
    job({ id: 3, kind: "analyze", status: "superseded", name: "граф" }),
  ];
  const api = fakeApi(routes({ [JOBS]: jobs, "POST /api/jobs/2/retry": job({ id: 2, status: "pending" }) }));
  await renderAt("/documents", api);
  const queue = section("Очередь");
  await within(queue).findByText("old.pdf");
  const headers = within(queue).getAllByRole("columnheader").map((th) => th.textContent);
  expect(headers.slice(0, 4)).toEqual(["Файл", "Что", "Статус", "Ход"]);
  expect(within(queue).getByRole("link", { name: "Смит.pdf" }).getAttribute("href")).toBe("/documents/d1");
  expect(within(queue).queryByRole("link", { name: "old.pdf" })).toBeNull();
  expect(within(queue).getByText("загрузка")).toBeTruthy();
  expect(within(queue).getByText("удаление")).toBeTruthy();
  expect(within(queue).getByText("анализ графа")).toBeTruthy();
  expect(within(queue).getByText("QdrantError")).toBeTruthy();
  expect(within(queue).getByText("заменена")).toBeTruthy();
  expect(within(queue).getAllByText("5 минут назад")).toHaveLength(2);

  const retry = within(queue).getAllByRole("button", { name: "Повторить" });
  expect(retry).toHaveLength(1);
  await click(retry[0]!);
  expect(api.requests()).toContain("POST /api/jobs/2/retry");
  await vi.waitFor(() => expect(count(api, JOBS)).toBe(2));
});

test("задача в работе: ход из progress, «обновляется» и опрос раз в 3000 мс", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const api = fakeApi(routes({ [JOBS]: [job({ id: 5, status: "running", progress: "embed 3/10", name: "b.pdf" })] }));
  await renderAt("/documents", api);
  await vi.waitFor(() => expect(screen.getByText("embed 3/10")).toBeTruthy());
  expect(within(section("Очередь")).getByText("обновляется")).toBeTruthy();
  expect(count(api, JOBS)).toBe(1);
  expect(count(api, DOCS)).toBe(1);

  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  await vi.waitFor(() => expect(count(api, JOBS)).toBe(2));
  await vi.waitFor(() => expect(count(api, DOCS)).toBe(2));
});

test("без активных задач опроса нет", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const api = fakeApi(routes({ [JOBS]: [job({ id: 5, status: "done", name: "b.pdf" })] }));
  await renderAt("/documents", api);
  await vi.waitFor(() => expect(screen.getByText("b.pdf")).toBeTruthy());
  await act(async () => {
    vi.advanceTimersByTime(10_000);
  });
  expect(count(api, JOBS)).toBe(1);
  expect(count(api, DOCS)).toBe(1);
});

test("опрос прекращается, когда задачи закончились", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let status: Job["status"] = "pending";
  const api = fakeApi(routes({ [JOBS]: () => Response.json([job({ id: 5, status, name: "b.pdf" })]) }));
  await renderAt("/documents", api);
  await vi.waitFor(() => expect(screen.getByText("обновляется")).toBeTruthy());
  status = "done";
  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  await vi.waitFor(() => expect(screen.queryByText("обновляется")).toBeNull());
  const jobs = count(api, JOBS);
  await act(async () => {
    vi.advanceTimersByTime(9000);
  });
  expect(count(api, JOBS)).toBe(jobs);
});

test("«Показать ещё»: страницы по 200 и проба следующей", async () => {
  const page = (from: number, n: number) => Array.from({ length: n }, (_, i) => doc(`d${from + i}`, `Документ ${from + i}`));
  const api = fakeApi(
    routes({
      [DOCS]: page(0, 200),
      "GET /api/documents?limit=1&offset=200": [doc("d200", "Документ 200")],
      "GET /api/documents?limit=200&offset=200": page(200, 3),
    }),
  );
  await renderAt("/documents", api);
  const more = await screen.findByRole("button", { name: "Показать ещё" });
  expect(screen.getByRole("link", { name: "Документ 199" })).toBeTruthy();
  await click(more);
  expect(await screen.findByRole("link", { name: "Документ 202" })).toBeTruthy();
  expect(api.requests()).toContain("GET /api/documents?limit=200&offset=200");
  expect(screen.queryByRole("button", { name: "Показать ещё" })).toBeNull();
});

test("ссылка на документ сохраняет панель на этой странице", async () => {
  const router = await renderAt("/documents?panel=entity%3Ae1", fakeApi(routes()));
  await screen.findByRole("link", { name: "Смит.pdf" });
  expect(router.state.location.search).toMatchObject({ panel: "entity:e1" });
});
