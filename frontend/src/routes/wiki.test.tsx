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
const item = (slug: string, title: string, entity_type: string) => ({
  page_id: `p-${slug}`,
  slug,
  title,
  entity_type,
  aliases: [],
});
const LIST = [item("rent", "Рента", "Concept"), item("adam-smith", "Адам Смит", "Person")];

const SMIT = encodeURIComponent("смит");
const C1 = "0123456789abcdef:00001";
const C2 = "0123456789abcdef:00002";
const PAGE = {
  id: "p-adam-smith",
  title: "Адам Смит",
  slug: "adam-smith",
  content: `---\ntitle: x\n---\n# Адам Смит\n\nСм. [[ricardo|Рикардо]]. Второе[^${C2}] и первое[^${C1}] и снова[^${C2}].\n\n## Sources\n\n[^${C1}]: a\n`,
  sources: { [C1]: "Смит.pdf" },
};

type Routes = Parameters<typeof fakeApi>[0];
const routes = (extra: Routes = {}): Routes => ({
  "GET /api/auth/me": ME,
  "GET /api/entities/types": TYPES,
  "GET /api/wiki?limit=200&offset=0": LIST,
  "GET /api/wiki/by-slug/adam-smith": PAGE,
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
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("индекс: группы по русским подписям в порядке каталога, ссылки на страницы", async () => {
  await renderAt("/wiki", fakeApi(routes()));
  const person = (await screen.findByRole("heading", { level: 2, name: "Человек" })).closest("section")!;
  const concept = screen.getByRole("heading", { level: 2, name: "Понятие" }).closest("section")!;
  expect(person.compareDocumentPosition(concept) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(person).getByRole("link", { name: "Адам Смит" }).getAttribute("href")).toBe("/wiki/adam-smith");
  expect(screen.getByText("2 страницы")).toBeTruthy();
  expect(screen.getByRole("searchbox", { name: "Поиск по wiki" })).toBeTruthy();
});

test("поиск «смит»: через 250 мс один запрос и ?q=", async () => {
  const api = fakeApi(routes({ [`GET /api/wiki?limit=200&offset=0&q=${SMIT}`]: [LIST[1]] }));
  const router = await renderAt("/wiki", api);
  await screen.findByRole("heading", { level: 2, name: "Понятие" });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const input = screen.getByRole("searchbox", { name: "Поиск по wiki" });
  fireEvent.change(input, { target: { value: "см" } });
  fireEvent.change(input, { target: { value: "смит" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(api.requests().filter((r) => r.includes("q="))).toHaveLength(0);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  vi.useRealTimers();
  await vi.waitFor(() => expect(screen.queryByRole("heading", { level: 2, name: "Понятие" })).toBeNull());
  expect(api.requests().filter((r) => r.includes("q="))).toEqual([`GET /api/wiki?limit=200&offset=0&q=${SMIT}`]);
  expect(router.state.location.search).toEqual({ q: "смит" });
  expect(screen.getByRole("heading", { level: 2, name: "Человек" })).toBeTruthy();
});

test("пусто: с запросом и без", async () => {
  await renderAt("/wiki?q=zzz", fakeApi(routes({ "GET /api/wiki?limit=200&offset=0&q=zzz": [] })));
  expect(await screen.findByText("Ничего не нашлось. Попробуйте другое слово или псевдоним.")).toBeTruthy();
});

test("пусто без запроса", async () => {
  await renderAt("/wiki", fakeApi(routes({ "GET /api/wiki?limit=200&offset=0": [] })));
  expect(await screen.findByText("Wiki пока пуста: страницы появятся после обработки документов.")).toBeTruthy();
});

test("«Показать ещё»: полная страница, счётчик с «первые»", async () => {
  const many = Array.from({ length: 200 }, (_, i) => item(`s${i}`, `Страница ${i}`, "Person"));
  const api = fakeApi(
    routes({
      "GET /api/wiki?limit=200&offset=0": many,
      "GET /api/wiki?limit=1&offset=200": [item("x", "X", "Person")],
      "GET /api/wiki?limit=200&offset=200": [item("x", "X", "Person")],
    }),
  );
  await renderAt("/wiki", api);
  expect(await screen.findByText("Показаны первые 200 страниц")).toBeTruthy();
  await click(screen.getByRole("button", { name: "Показать ещё" }));
  await vi.waitFor(() => expect(screen.getByText("201 страница")).toBeTruthy());
});

test("страница: ссылка [[slug|label]], сноски по первому появлению, источники", async () => {
  const router = await renderAt("/wiki/adam-smith", fakeApi(routes()));
  expect(await screen.findByRole("heading", { level: 1, name: "Адам Смит" })).toBeTruthy();
  expect(within(screen.getByRole("navigation", { name: "Путь" })).getByRole("link", { name: "Wiki" }).getAttribute("href")).toBe("/wiki");
  const link = screen.getByRole("link", { name: "Рикардо" });
  expect(link.getAttribute("href")).toBe("/wiki/ricardo");
  const list = screen.getByRole("list", { name: "Источники" });
  const rows = within(list).getAllByRole("listitem");
  // C2 встречается первым → №1 (источника нет), C1 → №2
  expect(rows[0]!.textContent).toBe("1фрагмент удалён");
  expect(rows[1]!.textContent).toBe("2Смит.pdf");
  expect(router.state.location.pathname).toBe("/wiki/adam-smith");
});

test("клик по сноске и по источнику открывает панель фрагмента; «Сущность» — сущности", async () => {
  const router = await renderAt("/wiki/adam-smith", fakeApi(routes({ [`GET /api/chunks/${C1}`]: {} })));
  await screen.findByRole("heading", { level: 1, name: "Адам Смит" });
  await click(screen.getByRole("button", { name: /Смит\.pdf/ }));
  expect(router.state.location.search).toEqual({ panel: `chunk:${C1}` });
  await click(screen.getByRole("button", { name: "Сущность" }));
  expect(router.state.location.search).toEqual({ panel: "entity:p-adam-smith" });
});

test("переход по ссылке внутри страницы без перезагрузки", async () => {
  const router = await renderAt(
    "/wiki/adam-smith",
    fakeApi(routes({ "GET /api/wiki/by-slug/ricardo": { ...PAGE, id: "p2", title: "Рикардо", slug: "ricardo", content: "# Рикардо\n\nТекст", sources: {} } })),
  );
  await click(await screen.findByRole("link", { name: "Рикардо" }));
  expect(router.state.location.pathname).toBe("/wiki/ricardo");
  expect(await screen.findByRole("heading", { level: 1, name: "Рикардо" })).toBeTruthy();
});

test("по pageId; 404 — текст про удалённую страницу", async () => {
  await renderAt("/wiki/page/zz", fakeApi(routes({ "GET /api/wiki/zz": () => fail(404, "нет") })));
  expect(await screen.findByText("Страница не найдена: возможно, её удалили вместе с документом.")).toBeTruthy();
  expect(within(screen.getByRole("navigation", { name: "Путь" })).getByRole("link", { name: "Wiki" }).getAttribute("href")).toBe("/wiki");
});

test("страница по pageId загружается", async () => {
  await renderAt("/wiki/page/p-adam-smith", fakeApi(routes({ "GET /api/wiki/p-adam-smith": PAGE })));
  expect(await screen.findByRole("heading", { level: 1, name: "Адам Смит" })).toBeTruthy();
});

test("адрес без ?q= очищает поле и не возвращает запрос; «назад» восстанавливает", async () => {
  const api = fakeApi(routes({ "GET /api/wiki?limit=200&offset=0&q=x": [LIST[1]] }));
  const router = await renderAt("/wiki?q=x", api);
  const input = (await screen.findByRole("searchbox", { name: "Поиск по wiki" })) as HTMLInputElement;
  expect(input.value).toBe("x");
  await act(async () => {
    await router.navigate({ href: "/wiki" });
  });
  await vi.waitFor(() => expect(input.value).toBe(""));
  await new Promise((r) => setTimeout(r, 400));
  expect(router.state.location.search).toEqual({});
  expect(api.requests().filter((r) => r.includes("q=x"))).toHaveLength(1);
  await act(async () => {
    router.history.back();
  });
  await vi.waitFor(() => expect(input.value).toBe("x"));
  expect(router.state.location.search).toEqual({ q: "x" });
});
