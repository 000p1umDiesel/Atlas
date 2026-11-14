import { createMemoryHistory } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const TYPES = [
  { type: "Person", label: "Человек", description: "", count: 2 },
  { type: "Organization", label: "Организация", description: "", count: 1 },
];
const card = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  entity_id: id,
  name,
  type: "Person",
  aliases: ["Ada Lovelace"],
  description: "Математик.\nПервая программистка.",
  descriptions: [],
  mention_count: 7,
  pagerank: 0.0123,
  community: "7",
  degree: 3,
  documents_count: 2,
  has_page: true,
  slug: "ada",
  ...extra,
});
const relation = (extra: Record<string, unknown> = {}) => ({
  src_id: "a",
  src_name: "Ада Лавлейс",
  src_type: "Person",
  predicate: "works_at",
  dst_id: "org",
  dst_name: "Аналитическое общество",
  dst_type: "Organization",
  description: "Работала вместе с Бэббиджем.",
  weight: 0.5,
  evidence: ["d1:00003", "d1:00004"],
  ...extra,
});
const RELATIONS = {
  total: 2,
  predicates: [
    { predicate: "works_at", count: 1 },
    { predicate: "knows", count: 1 },
  ],
  items: [
    relation(),
    relation({ src_id: "b", src_name: "Бэббидж", src_type: "Person", predicate: "knows", dst_id: "a", dst_name: "Ада Лавлейс", dst_type: "Person", weight: 2, evidence: [] }),
  ],
};
const COMMUNITIES = {
  ready: true,
  total: 1,
  items: [{ community: "7", size: 3, top: [{ entity_id: "a", name: "Ада Лавлейс", type: "Person", mention_count: 7, pagerank: 0.01, community: "7", degree: 3 }], types: {} }],
};
const MENTIONS = {
  total: 1,
  items: [
    {
      chunk_id: "d1:00012",
      doc_id: "d1",
      name: "Записки.pdf",
      text: "Тогда Ада Лавлейс написала программу.",
      page: 4,
      headings: ["Глава 1", "Машина"],
      url: null,
    },
  ],
};
const DOCUMENTS = [
  { doc_id: "d1", name: "Записки.pdf", mentions: 5, url: null },
  { doc_id: "d2", name: "Письма.docx", mentions: 1, url: null },
];
const rel = (id: string, query = "direction=both") => `GET /api/entities/${id}/relations?${query}&limit=50&offset=0`;

type Routes = Parameters<typeof fakeApi>[0];
const routes = (extra: Routes = {}): Routes => ({
  "GET /api/auth/me": ME,
  "GET /api/entities/types": TYPES,
  "GET /api/graph/communities?limit=12": { ready: false, total: 0, items: [] },
  "GET /api/graph/communities?limit=50": COMMUNITIES,
  "GET /api/entities/a": card("a", "Ада Лавлейс"),
  [rel("a")]: RELATIONS,
  "GET /api/entities/a/mentions?limit=20&offset=0": MENTIONS,
  "GET /api/entities/a/documents?limit=200": DOCUMENTS,
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

const main = () => screen.getByRole("main");
const metric = (label: string) => screen.queryByRole("group", { name: label });
const relationsPanel = () => screen.getByRole("tabpanel", { name: /Связи/ });
const findRelations = () => screen.findByRole("tabpanel", { name: /Связи/ });

beforeAll(() => {
  window.scrollTo = vi.fn();
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("заголовок: крошка, имя, чип типа, алиасы, описание, метрики и сообщество", async () => {
  await renderAt("/entities/a", fakeApi(routes()));
  expect(await screen.findByRole("heading", { level: 1, name: "Ада Лавлейс" })).toBeTruthy();
  const crumbs = screen.getByRole("navigation", { name: "Путь" });
  expect(within(crumbs).getByRole("link", { name: "Сущности" }).getAttribute("href")).toBe("/entities");
  expect(within(main()).getByText("Человек")).toBeTruthy();
  expect(screen.getByText("Также: Ada Lovelace")).toBeTruthy();
  expect(screen.getByText(/Первая программистка/).className).toContain("whitespace-pre-wrap");
  expect(within(metric("Упоминания")!).getByText("7")).toBeTruthy();
  expect(within(metric("Связи")!).getByText("3")).toBeTruthy();
  expect(within(metric("Документы")!).getByText("2")).toBeTruthy();
  expect(within(metric("PageRank")!).getByText("0,0123")).toBeTruthy();
  const community = await within(metric("Сообщество")!).findByRole("link", { name: "Ада Лавлейс" });
  expect(community.getAttribute("href")).toBe("/graph?mode=community&community=7");
  const tabs = screen.getByRole("tablist");
  expect(within(tabs).getAllByRole("tab").map((t) => t.textContent)).toEqual(["Связи 3", "Упоминания 7", "Документы 2", "Wiki"]);
  expect(within(tabs).getByRole("tab", { name: /Связи/ }).getAttribute("aria-selected")).toBe("true");
});

test("без анализа графа: нет метрик PageRank и «Сообщество»; без страницы — нет вкладки «Wiki»", async () => {
  const api = fakeApi(routes({ "GET /api/entities/a": card("a", "Ада Лавлейс", { pagerank: null, community: null, has_page: false, slug: null }) }));
  await renderAt("/entities/a", api);
  await screen.findByRole("heading", { level: 1, name: "Ада Лавлейс" });
  expect(metric("Упоминания")).toBeTruthy();
  expect(metric("PageRank")).toBeNull();
  expect(metric("Сообщество")).toBeNull();
  expect(screen.queryByRole("tab", { name: "Wiki" })).toBeNull();
  expect(api.requests()).not.toContain("GET /api/graph/communities?limit=50");
});

test("сообщество не в топе: подпись — его id", async () => {
  const api = fakeApi(routes({ "GET /api/entities/a": card("a", "Ада Лавлейс", { community: "42" }) }));
  await renderAt("/entities/a", api);
  await screen.findByRole("heading", { level: 1, name: "Ада Лавлейс" });
  const link = await within(metric("Сообщество")!).findByRole("link", { name: "42" });
  expect(link.getAttribute("href")).toBe("/graph?mode=community&community=42");
});

test("?tab=mentions открывает упоминания; клик по «Документы» — replace с tab=documents", async () => {
  const router = await renderAt("/entities/a?tab=mentions&panel=entity:a", fakeApi(routes()));
  const mentions = await screen.findByRole("tabpanel", { name: /Упоминания/ });
  await within(mentions).findByRole("link", { name: "Записки.pdf" });
  const before = router.history.length;
  await click(screen.getByRole("tab", { name: /Документы/ }));
  expect(router.state.location.search).toEqual({ tab: "documents", panel: "entity:a" });
  expect(router.history.length).toBe(before);
  const docs = await screen.findByRole("tabpanel", { name: /Документы/ });
  const link = await within(docs).findByRole("link", { name: "Записки.pdf" });
  expect(link.getAttribute("href")).toBe("/documents/d1");
  expect(within(docs).getByText("5 упоминаний")).toBeTruthy();
  expect(within(docs).getByText("1 упоминание")).toBeTruthy();
  await click(screen.getByRole("tab", { name: /Связи/ }));
  expect(router.state.location.search).toEqual({ panel: "entity:a" });
});

test("неизвестная вкладка и «wiki» без страницы — «Связи»; лишние параметры отбрасываются", async () => {
  const api = fakeApi(routes({ "GET /api/entities/a": card("a", "Ада Лавлейс", { has_page: false, slug: null }) }));
  const router = await renderAt("/entities/a?tab=wiki&foo=1&panel=entity:a", api);
  await within(await screen.findByRole("tabpanel", { name: /Связи/ })).findByRole("link", { name: "Бэббидж" });
  expect(screen.getByRole("tab", { name: /Связи/ }).getAttribute("aria-selected")).toBe("true");
  await click(screen.getByRole("tab", { name: /Упоминания/ }));
  expect(router.state.location.search).toEqual({ tab: "mentions", panel: "entity:a" });
  cleanup();
  await renderAt("/entities/a?tab=bogus", fakeApi(routes()));
  expect((await screen.findByRole("tab", { name: /Связи/ })).getAttribute("aria-selected")).toBe("true");
});

test("«Связи»: строки, направление, другая сторона, вес, предикаты и фрагменты", async () => {
  const api = fakeApi(
    routes({
      [rel("a", "direction=both&predicate=works_at")]: { ...RELATIONS, items: [relation()] },
      [rel("a", "direction=out&predicate=works_at")]: { ...RELATIONS, items: [relation()] },
      [rel("a", "direction=out")]: { ...RELATIONS, items: [relation()] },
    }),
  );
  const router = await renderAt("/entities/a", api);
  const other = await within(await findRelations()).findByRole("link", { name: "Аналитическое общество" });
  expect(other.getAttribute("href")).toBe("/entities/org");
  expect(within(relationsPanel()).getByRole("link", { name: "Бэббидж" }).getAttribute("href")).toBe("/entities/b");
  expect(within(relationsPanel()).getByText("Исходящая связь")).toBeTruthy();
  expect(within(relationsPanel()).getByText("Входящая связь")).toBeTruthy();
  expect(within(relationsPanel()).getByText("вес 0,5")).toBeTruthy();

  await click(within(relationsPanel()).getByRole("button", { name: "works at 1" }));
  await vi.waitFor(() => expect(api.requests()).toContain(rel("a", "direction=both&predicate=works_at")));
  expect(within(relationsPanel()).getByRole("button", { name: "works at 1" }).getAttribute("aria-pressed")).toBe("true");

  await click(within(relationsPanel()).getByRole("radio", { name: "Исходящие" }));
  await vi.waitFor(() => expect(api.requests()).toContain(rel("a", "direction=out&predicate=works_at")));

  await click(within(relationsPanel()).getByRole("button", { name: "works at 1" }));
  await vi.waitFor(() => expect(api.requests()).toContain(rel("a", "direction=out")));

  await click(within(relationsPanel()).getByRole("button", { name: "фрагмент 1" }));
  expect(router.state.location.search).toEqual({ panel: "chunk:d1:00003" });
});

test("«Связи»: «Показать ещё» и пустой список", async () => {
  const first = Array.from({ length: 50 }, (_, i) => relation({ dst_id: `x${i}`, dst_name: `Узел ${i}` }));
  const api = fakeApi(
    routes({
      [rel("a")]: { total: 51, predicates: [], items: first },
      "GET /api/entities/a/relations?direction=both&limit=50&offset=50": {
        total: 51,
        predicates: [],
        items: [relation({ dst_id: "last", dst_name: "Последний" })],
      },
      [rel("a", "direction=in")]: { total: 0, predicates: [], items: [] },
    }),
  );
  await renderAt("/entities/a", api);
  await within(await findRelations()).findByRole("link", { name: "Узел 49" });
  await click(within(relationsPanel()).getByRole("button", { name: "Показать ещё" }));
  await within(relationsPanel()).findByRole("link", { name: "Последний" });
  expect(within(relationsPanel()).queryByRole("button", { name: "Показать ещё" })).toBeNull();
  await click(within(relationsPanel()).getByRole("radio", { name: "Входящие" }));
  expect(await within(relationsPanel()).findByText("Связей нет.")).toBeTruthy();
});

test("«Упоминания»: ссылка на место в документе, страница, заголовки, <mark>, клик по карточке", async () => {
  const router = await renderAt("/entities/a?tab=mentions", fakeApi(routes()));
  const panel = await screen.findByRole("tabpanel", { name: /Упоминания/ });
  const link = await within(panel).findByRole("link", { name: "Записки.pdf" });
  expect(link.getAttribute("href")).toBe("/documents/d1/read?chunk=12");
  expect(within(panel).getByText("с. 4")).toBeTruthy();
  expect(within(panel).getByText("Глава 1 › Машина")).toBeTruthy();
  const mark = panel.querySelector("mark")!;
  expect(mark.textContent).toBe("Ада Лавлейс");
  await click(mark);
  expect(router.state.location.search).toEqual({ tab: "mentions", panel: "chunk:d1:00012" });
});

test("переход с /entities/a на /entities/b сбрасывает выбранный предикат", async () => {
  const api = fakeApi(
    routes({
      [rel("a", "direction=both&predicate=works_at")]: { ...RELATIONS, items: [relation()] },
      "GET /api/entities/b": card("b", "Бэббидж", { aliases: [] }),
      [rel("b")]: { total: 1, predicates: [{ predicate: "works_at", count: 1 }], items: [relation({ src_id: "b", src_name: "Бэббидж" })] },
    }),
  );
  const router = await renderAt("/entities/a", api);
  await click(await within(await findRelations()).findByRole("button", { name: "works at 1" }));
  await vi.waitFor(() => expect(api.requests()).toContain(rel("a", "direction=both&predicate=works_at")));
  await act(async () => {
    await router.navigate({ href: "/entities/b" });
  });
  await screen.findByRole("heading", { level: 1, name: "Бэббидж" });
  await within(await findRelations()).findByRole("button", { name: "works at 1" });
  const forB = api.requests().filter((r) => r.startsWith("GET /api/entities/b/relations"));
  expect(forB).toEqual([rel("b")]);
  expect(within(relationsPanel()).getByRole("button", { name: "works at 1" }).getAttribute("aria-pressed")).toBe("false");
});

const HEX = "0123456789abcdef";

test("вкладка «Wiki»: тело статьи и ссылка на страницу", async () => {
  const page = {
    id: "a",
    title: "Ада Лавлейс",
    slug: "ada",
    content: `---\ntitle: x\n---\nПервая программа[^${HEX}:00012].\n\n## Sources\n\n[^${HEX}:00012]: a\n`,
    sources: { [`${HEX}:00012`]: "Записки.pdf" },
  };
  await renderAt("/entities/a?tab=wiki", fakeApi(routes({ "GET /api/wiki/by-slug/ada": page })));
  const panel = await screen.findByRole("tabpanel", { name: "Wiki" });
  expect(await within(panel).findByText(/Первая программа/)).toBeTruthy();
  expect(within(within(panel).getByRole("list", { name: "Источники" })).getByText("Записки.pdf")).toBeTruthy();
  expect(within(panel).getByRole("link", { name: "Открыть страницу wiki" }).getAttribute("href")).toBe("/wiki/ada");
});

test("404: русский текст и путь к списку сущностей", async () => {
  await renderAt("/entities/gone", fakeApi(routes({ "GET /api/entities/gone": () => fail(404, "Сущность не найдена") })));
  expect(await screen.findByText("Сущность не найдена: возможно, её удалили вместе с документом.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "К списку сущностей" }).getAttribute("href")).toBe("/entities");
  expect(screen.queryByRole("tablist")).toBeNull();
});

test("колонка раздела — фильтры сущностей", async () => {
  await renderAt("/entities/a", fakeApi(routes()));
  await screen.findByRole("heading", { level: 1, name: "Ада Лавлейс" });
  const column = screen.getByRole("complementary", { name: "Контекст раздела" });
  expect(within(column).getByRole("navigation", { name: "Фильтры сущностей" })).toBeTruthy();
});
