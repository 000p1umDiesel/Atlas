import { act, screen } from "@testing-library/react";
import { fake2d } from "../test/fakeGraph";
import { fail, fakeApi } from "../test/fakeApi";
import { renderWithRouter } from "../test/router";
import { MiniGraph } from "./MiniGraph";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));

const node = (id: string, name: string) => ({
  entity_id: id,
  name,
  type: "Person",
  mention_count: 3,
  pagerank: null,
  community: null,
  degree: 1,
});
const NEIGHBORS = "GET /api/graph/e1/neighbors?depth=1&limit=40";
const RELATION = {
  src_id: "e1",
  src_name: "Ада",
  predicate: "knows",
  dst_id: "e2",
  dst_name: "Бэббидж",
  description: "",
  weight: 1,
  evidence: [],
};

afterEach(() => {
  vi.unstubAllGlobals();
  fake2d.last = null;
});

test("окрестность: запрос глубины 1 до 40 узлов, центр выбран, клик по соседу — его страница", async () => {
  const api = fakeApi({ [NEIGHBORS]: { nodes: [node("e1", "Ада"), node("e2", "Бэббидж")], relations: [RELATION] } });
  vi.stubGlobal("fetch", api);
  const { router } = await renderWithRouter(<MiniGraph id="e1" />, "/entities/e1");
  expect(screen.getByRole("heading", { name: "Окрестность" })).toBeTruthy();
  await screen.findByTestId("graph2d");
  expect(api.requests()).toContain(NEIGHBORS);
  expect(fake2d.last!.selected).toBe("e1");
  expect(fake2d.last!.graph.order).toBe(2);
  // клик по самому центру — никуда
  await act(async () => fake2d.last!.onSelect("e1"));
  expect(router.state.location.pathname).toBe("/entities/e1");
  await act(async () => {
    fake2d.last!.onSelect("e2");
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/entities/e2"));
});

test("без связей — «Связей пока нет.», холста нет", async () => {
  vi.stubGlobal("fetch", fakeApi({ [NEIGHBORS]: { nodes: [node("e1", "Ада")], relations: [] } }));
  await renderWithRouter(<MiniGraph id="e1" />, "/entities/e1");
  expect(await screen.findByText("Связей пока нет.")).toBeTruthy();
  expect(screen.queryByTestId("graph2d")).toBeNull();
});

test("связь с соседом вне nodes — не «Связей пока нет.»: граф рисует, что есть", async () => {
  // Сервер урезал узлы по пределу: конец связи e2 в nodes не попал.
  vi.stubGlobal("fetch", fakeApi({ [NEIGHBORS]: { nodes: [node("e1", "Ада")], relations: [RELATION] } }));
  await renderWithRouter(<MiniGraph id="e1" />, "/entities/e1");
  await screen.findByTestId("graph2d");
  expect(screen.queryByText("Связей пока нет.")).toBeNull();
  expect(fake2d.last!.graph.order).toBe(1);
});

test("ошибка запроса — текст сервера", async () => {
  vi.stubGlobal("fetch", fakeApi({ [NEIGHBORS]: () => fail(503, "Граф недоступен") }));
  await renderWithRouter(<MiniGraph id="e1" />, "/entities/e1");
  expect(await screen.findByText("Граф недоступен")).toBeTruthy();
});
