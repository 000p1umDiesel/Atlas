import { createMemoryHistory } from "@tanstack/react-router";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
vi.mock("../graph/Graph3D", async () => ({ default: (await import("../test/fakeGraph")).FakeGraph3D }));

const ME = { id: "u1", username: "owner" };
const ID = "0123456789abcdef:00007";
const CHUNK = {
  chunk_id: ID,
  doc_id: "0123456789abcdef",
  name: "отчёт.pdf",
  text: "Полный текст <b>фрагмента</b>",
  headings: ["Глава 1", "Раздел 2"],
  page: 4,
  url: "/api/documents/0123456789abcdef/content",
};
const ENTITY = {
  entity_id: "e1",
  name: "Адам Смит",
  type: "Person",
  aliases: ["Adam Smith"],
  description: "Экономист",
  descriptions: [],
  mention_count: 12,
  pagerank: 0.0412,
  community: "community:abc",
  degree: 7,
  documents_count: 3,
  has_page: true,
  slug: "adam-smith",
};
const TYPES = [{ type: "Person", label: "Человек", description: "", count: 1 }];

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  return router;
}

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("/chat?panel=chunk:… грузит фрагмент, Esc убирает panel из URL", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, [`GET /api/chunks/${encodeURIComponent(ID)}`]: CHUNK });
  const router = await renderAt(`/chat?panel=chunk:${ID}&x=1`, api);
  const panel = await screen.findByRole("dialog", { name: "Фрагмент" });
  expect(await within(panel).findByText("отчёт.pdf, стр. 4")).toBeTruthy();
  expect(within(panel).getByText("Глава 1 › Раздел 2")).toBeTruthy();
  // текст фрагмента — текстом, не разметкой
  expect(within(panel).getByText("Полный текст <b>фрагмента</b>")).toBeTruthy();
  expect(within(panel).getByRole("link", { name: "Открыть в документе" }).getAttribute("href")).toBe(
    "/documents/0123456789abcdef/read?chunk=7",
  );
  const download = within(panel).getByRole("link", { name: "Скачать исходник" });
  expect(download.getAttribute("href")).toBe(CHUNK.url);
  expect(download.hasAttribute("download")).toBe(true);

  const historyLength = router.history.length;
  fireEvent.keyDown(document, { key: "Escape" });
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ x: "1" }));
  expect(router.state.location.pathname).toBe("/chat");
  expect(router.history.length).toBe(historyLength); // replace, без новой записи
  await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: "Фрагмент" })).toBeNull());
});

test("фрагмент без исходника — без «Скачать исходник»; 404 — «Источник удалён»", async () => {
  const gone = "ffffffffffffffff:00001";
  const api = fakeApi({
    "GET /api/auth/me": ME,
    [`GET /api/chunks/${encodeURIComponent(ID)}`]: { ...CHUNK, url: null, page: null, headings: [] },
    [`GET /api/chunks/${encodeURIComponent(gone)}`]: () => fail(404, "Фрагмент не найден"),
  });
  const router = await renderAt(`/chat?panel=chunk:${ID}`, api);
  const panel = await screen.findByRole("dialog", { name: "Фрагмент" });
  expect(await within(panel).findByText("отчёт.pdf")).toBeTruthy();
  expect(within(panel).queryByRole("link", { name: "Скачать исходник" })).toBeNull();

  await router.navigate({ href: `/chat?panel=chunk:${gone}`, replace: true });
  expect(await within(panel).findByText("Источник удалён: документа больше нет в базе.")).toBeTruthy();
});

test("панель сущности: тип по-русски, метрики, ссылка на wiki; крестик закрывает", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, "GET /api/entities/e1": ENTITY, "GET /api/entities/types": TYPES });
  const router = await renderAt("/documents?panel=entity:e1", api);
  const panel = await screen.findByRole("dialog", { name: "Сущность" });
  expect(await within(panel).findByRole("heading", { name: "Адам Смит" })).toBeTruthy();
  expect(await within(panel).findByText("Человек")).toBeTruthy();
  expect(within(panel).getByText("Adam Smith", { exact: false })).toBeTruthy();
  expect(within(panel).getByText("Экономист")).toBeTruthy();
  expect(within(within(panel).getByRole("group", { name: "Упоминания" })).getByText("12")).toBeTruthy();
  expect(within(within(panel).getByRole("group", { name: "Связи" })).getByText("7")).toBeTruthy();
  expect(within(within(panel).getByRole("group", { name: "Документы" })).getByText("3")).toBeTruthy();
  expect(within(panel).getByRole("group", { name: "PageRank" })).toBeTruthy();
  expect(within(panel).getByRole("link", { name: "Страница wiki" }).getAttribute("href")).toBe("/wiki/adam-smith");

  fireEvent.click(within(panel).getByRole("button", { name: "Закрыть" }));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({}));
});

test("сущность без анализа и без страницы: нет PageRank, сообщества и ссылки на wiki", async () => {
  const api = fakeApi({
    "GET /api/auth/me": ME,
    "GET /api/entities/e1": { ...ENTITY, pagerank: null, community: null, has_page: false, slug: null },
  });
  await renderAt("/documents?panel=entity:e1", api);
  const panel = await screen.findByRole("dialog", { name: "Сущность" });
  await within(panel).findByRole("heading", { name: "Адам Смит" });
  expect(within(panel).queryByRole("group", { name: "PageRank" })).toBeNull();
  expect(within(panel).queryByRole("link", { name: /Сообщество/ })).toBeNull();
  expect(within(panel).queryByRole("link", { name: "Страница wiki" })).toBeNull();
});

test("404 сущности — «Сущность не найдена»", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, "GET /api/entities/zz": () => fail(404, "Сущность не найдена") });
  await renderAt("/documents?panel=entity:zz", api);
  const panel = await screen.findByRole("dialog", { name: "Сущность" });
  expect(await within(panel).findByText("Сущность не найдена")).toBeTruthy();
});

test("неверный panel — панели нет", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME });
  await renderAt("/documents?panel=x:1", api);
  await screen.findAllByRole("heading", { name: "Документы" });
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("на /graph панель не монтируется", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, [`GET /api/chunks/${encodeURIComponent(ID)}`]: CHUNK });
  await renderAt(`/graph?panel=chunk:${ID}`, api);
  await screen.findByRole("heading", { name: "Граф" });
  expect(screen.queryByRole("dialog", { name: "Фрагмент" })).toBeNull();
  expect(api.requests().some((r) => r.includes("/api/chunks/"))).toBe(false);
});
