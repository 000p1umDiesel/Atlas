import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { deferred, fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const TYPES = [
  { type: "Person", label: "Человек", description: "", count: 2 },
  { type: "Work", label: "Произведение", description: "", count: 0 },
  { type: "Concept", label: "Понятие", description: "", count: 1 },
];
const entity = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  entity_id: id,
  name,
  type: "Person",
  mention_count: 3,
  pagerank: null,
  community: null,
  degree: 2,
  aliases: [],
  has_page: false,
  slug: null,
  ...extra,
});
const page = (items: unknown[], total = items.length) => ({ total, items });
const COMMUNITIES = {
  ready: true,
  total: 2,
  items: [
    { community: "7", size: 12, top: [entity("a", "Ада Лавлейс"), entity("b", "Бэббидж"), entity("c", "Машина"), entity("d", "Лишний")], types: { Person: 2 } },
    { community: "9", size: 4, top: [entity("e", "Рента")], types: { Concept: 1 } },
  ],
};
const ALL = "GET /api/entities?limit=50&offset=0";

type Routes = Parameters<typeof fakeApi>[0];
const routes = (extra: Routes = {}): Routes => ({
  "GET /api/auth/me": ME,
  "GET /api/entities/types": TYPES,
  "GET /api/graph/communities?limit=12": COMMUNITIES,
  [ALL]: page([entity("e1", "Ада Лавлейс", { aliases: ["Ada Lovelace"], has_page: true, slug: "ada" })]),
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

const entityRequests = (api: FakeApi) => api.requests().filter((r) => r.startsWith("GET /api/entities?"));
const column = () => screen.getByRole("complementary", { name: "Контекст раздела" });
const table = () => screen.getByRole("table", { name: "Сущности" });

/**
 * Точка типа видна: строчный `<span>` с одним лишь size-2.5 в jsdom не проверить, а в браузере
 * он 0×0. Точка — блочная (block / inline-block), её обёртка — flex или блок.
 */
function expectVisibleDot(scope: HTMLElement) {
  const dot = scope.querySelector<HTMLElement>('span[aria-hidden="true"][style*="background-color"]');
  expect(dot).not.toBeNull();
  expect(dot!.className.split(/\s+/).some((c) => c === "block" || c === "inline-block")).toBe(true);
  expect(dot!.parentElement!.className.split(/\s+/).some((c) => c === "flex" || c === "block")).toBe(true);
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

test("каталог: заголовок, счётчик, строка с алиасом, ссылкой и значком wiki", async () => {
  await renderAt("/entities", fakeApi(routes()));
  expect(await screen.findByText("1 сущность")).toBeTruthy();
  expect(screen.getByRole("heading", { level: 1, name: "Сущности" })).toBeTruthy();
  const row = within(table()).getByRole("link", { name: "Ада Лавлейс" }).closest("tr")!;
  expect(within(row).getByRole("link", { name: "Ада Лавлейс" }).getAttribute("href")).toBe("/entities/e1");
  expect(within(row).getByText("Ada Lovelace")).toBeTruthy();
  expect(within(row).getByText("Человек")).toBeTruthy();
  expect(within(row).getByRole("link", { name: "Страница wiki" }).getAttribute("href")).toBe("/wiki/ada");
  expect(screen.getByRole("searchbox", { name: "Поиск сущностей" })).toBeTruthy();
  expectVisibleDot(row);
});

test("?q=ada&type=Person → один запрос с повтором типа, limit и offset", async () => {
  const api = fakeApi(routes({ "GET /api/entities?q=ada&type=Person&limit=50&offset=0": page([entity("e1", "Ada")]) }));
  await renderAt("/entities?q=ada&type=Person", api);
  await within(await screen.findByRole("table", { name: "Сущности" })).findByRole("link", { name: "Ada" });
  await new Promise((r) => setTimeout(r, 50));
  expect(entityRequests(api)).toEqual(["GET /api/entities?q=ada&type=Person&limit=50&offset=0"]);
});

test("«Показать ещё»: следующая страница с offset=50, пока не набрано total", async () => {
  const first = Array.from({ length: 50 }, (_, i) => entity(`a${i}`, `Сущность ${i}`));
  const second = Array.from({ length: 50 }, (_, i) => entity(`b${i}`, `Вторая ${i}`));
  const api = fakeApi(routes({ [ALL]: page(first, 120), "GET /api/entities?limit=50&offset=50": page(second, 120) }));
  await renderAt("/entities", api);
  expect(await screen.findByText("120 сущностей")).toBeTruthy();
  await click(screen.getByRole("button", { name: "Показать ещё" }));
  await within(table()).findByRole("link", { name: "Вторая 49" });
  expect(entityRequests(api)).toEqual([ALL, "GET /api/entities?limit=50&offset=50"]);
  expect(within(table()).getAllByRole("row")).toHaveLength(101);
  expect(screen.getByRole("button", { name: "Показать ещё" })).toBeTruthy();
});

test("всё набрано — «Показать ещё» нет", async () => {
  await renderAt("/entities", fakeApi(routes()));
  await screen.findByText("1 сущность");
  expect(screen.queryByRole("button", { name: "Показать ещё" })).toBeNull();
});

test("быстрый ввод: показан ответ на последний запрос, хотя прежний пришёл позже", async () => {
  const late = deferred<Response>();
  const api = fakeApi(
    routes({
      "GET /api/entities?q=al&limit=50&offset=0": () => late.promise,
      "GET /api/entities?q=alan&limit=50&offset=0": page([entity("t", "Alan Turing")]),
    }),
  );
  const router = await renderAt("/entities", api);
  const input = (await screen.findByRole("searchbox", { name: "Поиск сущностей" })) as HTMLInputElement;
  await screen.findByText("1 сущность");
  fireEvent.change(input, { target: { value: "al" } });
  await vi.waitFor(() => expect(entityRequests(api)).toContain("GET /api/entities?q=al&limit=50&offset=0"));
  fireEvent.change(input, { target: { value: "alan" } });
  await within(table()).findByRole("link", { name: "Alan Turing" });
  await act(async () => {
    late.resolve(Response.json(page([entity("x", "Alice")])));
    await new Promise((r) => setTimeout(r, 20));
  });
  expect(within(table()).queryByRole("link", { name: "Alice" })).toBeNull();
  expect(within(table()).getByRole("link", { name: "Alan Turing" })).toBeTruthy();
  expect(router.state.location.search).toEqual({ q: "alan" });
  expect(input.value).toBe("alan");
});

test("поиск ждёт 250 мс и пишет ?q=", async () => {
  const api = fakeApi(routes({ "GET /api/entities?q=ada&limit=50&offset=0": page([]) }));
  const router = await renderAt("/entities", api);
  const input = await screen.findByRole("searchbox", { name: "Поиск сущностей" });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  fireEvent.change(input, { target: { value: "ada" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(entityRequests(api).filter((r) => r.includes("q="))).toHaveLength(0);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  vi.useRealTimers();
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ q: "ada" }));
});

test("PageRank нет ни у кого — столбца нет, а сортировка по PageRank всё равно уходит на сервер", async () => {
  const api = fakeApi(routes({ "GET /api/entities?sort=pagerank&limit=50&offset=0": page([entity("e2", "Бэббидж")]) }));
  const router = await renderAt("/entities", api);
  await screen.findByText("1 сущность");
  expect(within(table()).queryByRole("columnheader", { name: /PageRank/ })).toBeNull();
  const sort = screen.getByRole("combobox", { name: "Сортировка" }) as HTMLSelectElement;
  expect([...sort.options].map((o) => o.textContent)).toEqual([
    "По упоминаниям",
    "По PageRank",
    "По связям",
    "По имени",
  ]);
  await act(async () => {
    fireEvent.change(sort, { target: { value: "pagerank" } });
    await new Promise((r) => setTimeout(r, 0));
  });
  await within(table()).findByRole("link", { name: "Бэббидж" });
  expect(router.state.location.search).toEqual({ sort: "pagerank" });
  expect(entityRequests(api)).toContain("GET /api/entities?sort=pagerank&limit=50&offset=0");
});

test("PageRank есть — столбец с числом; при q есть «По релевантности»", async () => {
  await renderAt(
    "/entities?q=ada",
    fakeApi(routes({ "GET /api/entities?q=ada&limit=50&offset=0": page([entity("e1", "Ada", { pagerank: 0.01234 })]) })),
  );
  await within(await screen.findByRole("table", { name: "Сущности" })).findByRole("link", { name: "Ada" });
  expect(within(table()).getByRole("columnheader", { name: /PageRank/ })).toBeTruthy();
  expect(within(table()).getByText("0,0123")).toBeTruthy();
  const sort = screen.getByRole("combobox", { name: "Сортировка" }) as HTMLSelectElement;
  expect(sort.value).toBe("relevance");
  expect(sort.options[0]!.textContent).toBe("По релевантности");
});

test("заголовок столбца сортирует: aria-sort, повторный клик меняет порядок", async () => {
  const api = fakeApi(
    routes({
      "GET /api/entities?sort=degree&limit=50&offset=0": page([entity("e1", "Ада Лавлейс")]),
      "GET /api/entities?sort=degree&order=asc&limit=50&offset=0": page([entity("e1", "Ада Лавлейс")]),
    }),
  );
  const router = await renderAt("/entities", api);
  await screen.findByText("1 сущность");
  const mentions = within(table()).getByRole("columnheader", { name: /Упоминания/ });
  expect(mentions.getAttribute("aria-sort")).toBe("descending");
  await click(within(within(table()).getByRole("columnheader", { name: /Связи/ })).getByRole("button"));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ sort: "degree" }));
  expect(within(table()).getByRole("columnheader", { name: /Связи/ }).getAttribute("aria-sort")).toBe("descending");
  expect(within(table()).getByRole("columnheader", { name: /Упоминания/ }).getAttribute("aria-sort")).toBe("none");
  await click(within(within(table()).getByRole("columnheader", { name: /Связи/ })).getByRole("button"));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ sort: "degree", order: "asc" }));
  expect(within(table()).getByRole("columnheader", { name: /Связи/ }).getAttribute("aria-sort")).toBe("ascending");
});

test("клик по имени и по строке → /entities/<id>", async () => {
  const router = await renderAt("/entities", fakeApi(routes()));
  await click(await within(await screen.findByRole("table", { name: "Сущности" })).findByRole("link", { name: "Ада Лавлейс" }));
  expect(router.state.location.pathname).toBe("/entities/e1");
  await act(async () => {
    await router.navigate({ href: "/entities" });
  });
  const row = (await within(await screen.findByRole("table", { name: "Сущности" })).findByRole("link", { name: "Ада Лавлейс" })).closest("tr")!;
  await click(within(row).getByText("Человек"));
  expect(router.state.location.pathname).toBe("/entities/e1");
});

test("пусто с фильтром: «Ничего не нашлось…» и сброс", async () => {
  const api = fakeApi(routes({ "GET /api/entities?type=Concept&sort=name&limit=50&offset=0": page([]) }));
  const router = await renderAt("/entities?type=Concept&sort=name", api);
  expect(await screen.findByText("Ничего не нашлось. Ослабьте фильтры.")).toBeTruthy();
  await click(screen.getByRole("button", { name: "Сбросить фильтры" }));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ sort: "name" }));
});

test("пусто без фильтров: ссылка на документы", async () => {
  await renderAt("/entities", fakeApi(routes({ [ALL]: page([]) })));
  const note = await screen.findByText((_, el) => el?.textContent === "Сущностей пока нет: загрузите документы.");
  expect(within(note).getByRole("link").getAttribute("href")).toBe("/documents");
});

test("ошибка API — текст сервера", async () => {
  await renderAt("/entities", fakeApi(routes({ [ALL]: () => Response.json({ detail: "Неверная сортировка" }, { status: 422 }) })));
  expect(await screen.findByText("Неверная сортировка")).toBeTruthy();
});

test("panel сохраняется при смене сортировки, мусор в адресе отбрасывается", async () => {
  const api = fakeApi(routes({ "GET /api/entities?sort=degree&limit=50&offset=0": page([]) }));
  const router = await renderAt("/entities?panel=entity:e1&sort=bogus&foo=1", api);
  await screen.findByText("1 сущность");
  expect(entityRequests(api)).toEqual([ALL]);
  await act(async () => {
    fireEvent.change(screen.getByRole("combobox", { name: "Сортировка" }), { target: { value: "degree" } });
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ panel: "entity:e1", sort: "degree" }));
});

test("колонка: только типы с сущностями, клик ставит и снимает ?type=", async () => {
  const api = fakeApi(routes({ "GET /api/entities?type=Concept&limit=50&offset=0": page([]) }));
  const router = await renderAt("/entities", api);
  const concept = await within(column()).findByRole("button", { name: /Понятие/ });
  expect(within(column()).queryByRole("button", { name: /Произведение/ })).toBeNull();
  expect(within(column()).getByRole("button", { name: /Человек/ }).textContent).toContain("2");
  await click(concept);
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ type: "Concept" }));
  expect(within(column()).getByRole("button", { name: /Понятие/ }).getAttribute("aria-pressed")).toBe("true");
  await click(within(column()).getByRole("button", { name: /Понятие/ }));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({}));
});

test("колонка: сообщества — три имени и размер; клик ставит и снимает ?community=", async () => {
  const api = fakeApi(routes({ "GET /api/entities?community=7&limit=50&offset=0": page([]) }));
  const router = await renderAt("/entities", api);
  const first = await within(column()).findByRole("button", { name: /Ада Лавлейс, Бэббидж, Машина/ });
  expect(first.textContent).not.toContain("Лишний");
  expect(first.textContent).toContain("12");
  await click(first);
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ community: "7" }));
  await click(within(column()).getByRole("button", { name: /Ада Лавлейс, Бэббидж, Машина/ }));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({}));
  expect(within(column()).getByRole("link", { name: "Все сообщества на графе" }).getAttribute("href")).toBe(
    "/graph?mode=community",
  );
});

test("колонка: анализа не было — подсказка", async () => {
  await renderAt("/entities", fakeApi(routes({ "GET /api/graph/communities?limit=12": { ready: false, total: 0, items: [] } })));
  expect(await within(column()).findByText("Сообщества появятся после анализа графа.")).toBeTruthy();
});

test("страж внизу: видимый — подгружает следующую страницу без кнопки", async () => {
  let notify: ((entries: { isIntersecting: boolean }[]) => void) | null = null;
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(cb: (entries: { isIntersecting: boolean }[]) => void) {
        notify = cb;
      }
      observe() {}
      disconnect() {}
    },
  );
  const first = Array.from({ length: 50 }, (_, i) => entity(`a${i}`, `Сущность ${i}`));
  const api = fakeApi(routes({ [ALL]: page(first, 51), "GET /api/entities?limit=50&offset=50": page([entity("z", "Последняя")], 51) }));
  await renderAt("/entities", api);
  await screen.findByText("51 сущность");
  await vi.waitFor(() => expect(notify).not.toBeNull());
  await act(async () => {
    notify!([{ isIntersecting: true }]);
    await new Promise((r) => setTimeout(r, 0));
  });
  await within(table()).findByRole("link", { name: "Последняя" });
  expect(screen.queryByRole("button", { name: "Показать ещё" })).toBeNull();
});

test("узкий экран: карточки вместо таблицы", async () => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  await renderAt("/entities", fakeApi(routes()));
  const cards = await screen.findByRole("list", { name: "Сущности" });
  expect(within(cards).getByRole("link", { name: "Ада Лавлейс" }).getAttribute("href")).toBe("/entities/e1");
  expect(within(cards).getByText(/Человек · 3 упоминания · 2 связи/)).toBeTruthy();
  expect(screen.queryByRole("table", { name: "Сущности" })).toBeNull();
  expectVisibleDot(within(cards).getByRole("listitem"));
});
