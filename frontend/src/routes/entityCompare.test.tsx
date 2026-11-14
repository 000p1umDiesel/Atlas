import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";
import { askHowRelated, validateCompareSearch } from "./entityCompare";

const ME = { id: "u1", username: "owner" };
const TYPES = [
  { type: "Person", label: "Человек", description: "", count: 2 },
  { type: "Equipment", label: "Оборудование", description: "", count: 1 },
];
const card = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  entity_id: id,
  name,
  type: "Person",
  aliases: [],
  description: "",
  descriptions: [],
  mention_count: 7,
  pagerank: 0.0123,
  community: "7",
  degree: 3,
  documents_count: 2,
  has_page: false,
  slug: null,
  ...extra,
});
const ADA = card("ada", "Ада Лавлейс");
const ENGINE = card("engine", "Аналитическая машина", { type: "Equipment", mention_count: 4 });
const COMPARISON = {
  a: ADA,
  b: ENGINE,
  direct: [
    {
      src_id: "ada",
      src_name: "Ада Лавлейс",
      predicate: "described",
      dst_id: "engine",
      dst_name: "Аналитическая машина",
      description: "Ада описала машину в заметках.",
      weight: 4,
      evidence: [],
    },
  ],
  shared_neighbors: {
    total: 2,
    items: [
      {
        entity_id: "babbage",
        name: "Бэббидж",
        type: "Person",
        a_predicates: [{ predicate: "collaborated_with", outgoing: true }],
        b_predicates: [{ predicate: "designed", outgoing: false }],
        weight: 11,
      },
    ],
  },
  neighbors: { a: 3, b: 3 },
  shared_documents: [{ doc_id: "d1", name: "Записки.pdf", a_mentions: 2, b_mentions: 5, url: null }],
  co_mentions: 1,
};
const COMMUNITIES = { ready: true, total: 0, items: [] };

type Routes = Parameters<typeof fakeApi>[0];
const routes = (extra: Routes = {}): Routes => ({
  "GET /api/auth/me": ME,
  "GET /api/entities/types": TYPES,
  "GET /api/graph/communities?limit=12": COMMUNITIES,
  "GET /api/graph/communities?limit=50": COMMUNITIES,
  "GET /api/entities/ada": ADA,
  "GET /api/entities/engine": ENGINE,
  "GET /api/entities/compare?a=ada&b=engine": COMPARISON,
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

test("validateCompareSearch — только непустые a, b и panel", () => {
  expect(validateCompareSearch({ a: "ada", b: " ", panel: "entity:x", tab: "y", c: 1 })).toEqual({ a: "ada", panel: "entity:x" });
  expect(askHowRelated("А", "Б")).toBe("Как связаны «А» и «Б»?");
});

test("без второй сущности — подсказка, запроса сравнения нет", async () => {
  const api = fakeApi(routes());
  await renderAt("/entities/compare?a=ada", api);
  expect(await screen.findByRole("heading", { level: 1, name: "Сравнение сущностей" })).toBeTruthy();
  expect(screen.getByText(/Выберите две сущности/)).toBeTruthy();
  expect(api.requests().some((r) => r.includes("/api/entities/compare"))).toBe(false);
});

test("две сущности: карточки, итог, прямая связь, общие соседи и документы, действия", async () => {
  const router = await renderAt("/entities/compare?a=ada&b=engine", fakeApi(routes()));
  const left = await screen.findByRole("article", { name: "Ада Лавлейс" });
  expect(within(left).getByRole("link", { name: "Ада Лавлейс" }).getAttribute("href")).toBe("/entities/ada");
  expect(screen.getByRole("article", { name: "Аналитическая машина" })).toBeTruthy();
  // Одно сообщество у обеих — подсвечено.
  expect(screen.getAllByText("Обе в одном сообществе")).toHaveLength(2);
  expect(screen.getByText("1 прямая связь · 2 общих соседа · 1 общий документ · вместе в 1 фрагменте")).toBeTruthy();

  const direct = screen.getByRole("region", { name: "Прямые связи" });
  expect(within(direct).getByText("Ада описала машину в заметках.")).toBeTruthy();

  const shared = screen.getByRole("region", { name: "Общие соседи" });
  const row = within(shared).getByRole("row", { name: /Бэббидж/ });
  expect(within(row).getByText("collaborated with")).toBeTruthy();
  expect(within(row).getByLabelText("к соседу")).toBeTruthy();
  expect(within(row).getByLabelText("от соседа")).toBeTruthy();
  expect(within(shared).getByText(/Показаны 1 из 2/)).toBeTruthy();

  const docs = screen.getByRole("region", { name: "Общие документы" });
  expect(within(docs).getByRole("link", { name: "Записки.pdf" }).getAttribute("href")).toBe("/documents/d1");

  const actions = screen.getByRole("group", { name: "Действия" });
  const ask = within(actions).getByRole("link", { name: "Спросить, как связаны" }).getAttribute("href")!;
  expect(new URLSearchParams(ask.split("?")[1]).get("ask")).toBe("Как связаны «Ада Лавлейс» и «Аналитическая машина»?");
  expect(within(actions).getByRole("link", { name: "Путь на графе" }).getAttribute("href")).toBe("/graph?mode=path&source=ada&target=engine");

  await click(screen.getByRole("button", { name: "Поменять местами" }));
  expect(router.state.location.search).toEqual({ a: "engine", b: "ada" });
});

test("без прямой связи — кратчайший путь", async () => {
  const path = {
    paths: [
      {
        nodes: [
          { entity_id: "ada", name: "Ада Лавлейс", type: "Person", mention_count: 1, pagerank: null, community: null, degree: 1 },
          { entity_id: "babbage", name: "Бэббидж", type: "Person", mention_count: 1, pagerank: null, community: null, degree: 2 },
          { entity_id: "engine", name: "Аналитическая машина", type: "Equipment", mention_count: 1, pagerank: null, community: null, degree: 1 },
        ],
        relations: [
          { src_id: "ada", src_name: "Ада Лавлейс", predicate: "knows", dst_id: "babbage", dst_name: "Бэббидж", description: "", weight: 1, evidence: [] },
          { src_id: "babbage", src_name: "Бэббидж", predicate: "designed", dst_id: "engine", dst_name: "Аналитическая машина", description: "", weight: 1, evidence: [] },
        ],
      },
    ],
    timed_out: false,
  };
  await renderAt(
    "/entities/compare?a=ada&b=engine",
    fakeApi(
      routes({
        "GET /api/entities/compare?a=ada&b=engine": { ...COMPARISON, direct: [] },
        "GET /api/graph/path?source=ada&target=engine&max_hops=4&limit=5": path,
      }),
    ),
  );
  const section = await screen.findByRole("region", { name: "Кратчайший путь" });
  expect(await within(section).findByRole("link", { name: "Бэббидж" })).toBeTruthy();
  expect(within(section).getByText("— designed —")).toBeTruthy();
  expect(screen.getByText(/^нет прямой связи/)).toBeTruthy();
});

test("одна сущность пропала — понятная ошибка", async () => {
  await renderAt(
    "/entities/compare?a=ada&b=engine",
    fakeApi(routes({ "GET /api/entities/compare?a=ada&b=engine": () => fail(404, "Сущность не найдена") })),
  );
  expect(await screen.findByText(/Одна из сущностей не найдена/)).toBeTruthy();
});

test("страница сущности ведёт к сравнению с ней", async () => {
  await renderAt("/entities/ada", fakeApi(routes()));
  const link = await screen.findByRole("link", { name: "Сравнить с…" });
  expect(link.getAttribute("href")).toBe("/entities/compare?a=ada");
});
