import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fakeApi, fail, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const TYPES = [
  { type: "Person", label: "Человек", description: "", count: 2 },
  { type: "Concept", label: "Понятие", description: "", count: 1 },
];

const stage = (name: string, over: object = {}) => ({
  stage: name,
  status: "done",
  attempts: 1,
  error: null,
  updated_at: "2026-11-10T10:00:00Z",
  ...over,
});

const DETAIL = {
  id: "d1",
  name: "Смит.pdf",
  status: "failed",
  uploaded: true,
  url: "/api/documents/d1/content",
  stages: [stage("parse"), stage("chunk"), stage("embed"), stage("extract", { status: "failed", attempts: 3, error: "LLMError" }), stage("graph", { status: "pending", attempts: 0, updated_at: null })],
  chunks: 12,
  entities: [
    { entity_id: "e1", name: "Адам Смит", type: "Person", has_page: true, slug: "adam-smith" },
    { entity_id: "e2", name: "Рикардо", type: "Person", has_page: false, slug: null },
    { entity_id: "e3", name: "Рента", type: "Concept", has_page: false, slug: null },
  ],
};

type Routes = Parameters<typeof fakeApi>[0];
const routes = (extra: Routes = {}): Routes => ({
  "GET /api/auth/me": ME,
  "GET /api/entities/types": TYPES,
  "GET /api/documents/d1": DETAIL,
  "GET /api/documents?limit=200&offset=0": [],
  "GET /api/jobs?limit=30": [],
  ...extra,
});

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

beforeAll(() => {
  window.scrollTo = vi.fn();
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("карточка: название, счётчики, стадии с ошибкой у extract", async () => {
  await renderAt("/documents/d1", fakeApi(routes()));
  expect(await screen.findByRole("heading", { level: 1, name: "Смит.pdf" })).toBeTruthy();
  expect(screen.getByText("12 фрагментов, 3 сущности")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Читать" }).getAttribute("href")).toBe("/documents/d1/read");
  expect(screen.getByRole("link", { name: "Скачать" }).getAttribute("href")).toBe("/api/documents/d1/content");
  const row = screen.getByText("Извлечение сущностей").closest("tr")!;
  expect(within(row).getByText("LLMError")).toBeTruthy();
  expect(within(row).getByText("3")).toBeTruthy();
  expect(screen.getByText("Запись в граф").closest("tr")!.textContent).toContain("—");
});

test("карточка: пустые стадии — пояснение про очередь", async () => {
  await renderAt("/documents/d1", fakeApi(routes({ "GET /api/documents/d1": { ...DETAIL, stages: [], chunks: 0, entities: [], url: null } })));
  expect(await screen.findByText("Документ ждёт своей очереди: worker ещё не брал его в работу.")).toBeTruthy();
  expect(screen.getByText("Сущностей пока нет.")).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Читать" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Скачать" })).toBeNull();
});

test("сущности сгруппированы под русскими подписями; клик открывает панель; wiki-ссылка", async () => {
  const router = await renderAt("/documents/d1", fakeApi(routes()));
  const person = (await screen.findByRole("heading", { level: 3, name: "Человек" })).closest("div")!;
  expect(screen.getByRole("heading", { level: 3, name: "Понятие" })).toBeTruthy();
  expect(within(person).getByRole("button", { name: "Рикардо" })).toBeTruthy();
  expect(within(person).getAllByRole("link", { name: "wiki" })).toHaveLength(1);
  expect(within(person).getByRole("link", { name: "wiki" }).getAttribute("href")).toBe("/wiki/adam-smith");
  await click(within(person).getByRole("button", { name: "Адам Смит" }));
  expect(router.state.location.search).toEqual({ panel: "entity:e1" });
});

test("удаление: DELETE и переход к списку", async () => {
  const api = fakeApi(routes({ "DELETE /api/documents/d1": { id: 1 } }));
  const router = await renderAt("/documents/d1", api);
  await click(await screen.findByRole("button", { name: "Удалить" }));
  await click(screen.getByRole("button", { name: "Удалить из базы?" }));
  expect(api.requests()).toContain("DELETE /api/documents/d1");
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/documents"));
});

test("«Читать» скрыта при chunks: 0", async () => {
  await renderAt("/documents/d1", fakeApi(routes({ "GET /api/documents/d1": { ...DETAIL, chunks: 0 } })));
  await screen.findByRole("heading", { level: 1, name: "Смит.pdf" });
  expect(screen.queryByRole("link", { name: "Читать" })).toBeNull();
});

test("404: текст ошибки и ссылка к списку", async () => {
  await renderAt("/documents/zz", fakeApi(routes({ "GET /api/documents/zz": () => fail(404, "Документ не найден") })));
  expect(await screen.findByText("Документ не найден")).toBeTruthy();
  expect(screen.getByRole("link", { name: "К списку документов" }).getAttribute("href")).toBe("/documents");
});
