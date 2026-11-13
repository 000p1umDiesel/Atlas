import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { generateFixtures } from "../fixtures/generate";
import { createAppRouter } from "../router";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
vi.mock("../graph/Graph3D", async () => ({ default: (await import("../test/fakeGraph")).FakeGraph3D }));

async function renderHome() {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  await screen.findByRole("list", { name: "Сущности по типам" });
  return router;
}

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});

beforeEach(() => vi.stubEnv("VITE_FIXTURES", "1"));
afterEach(() => vi.unstubAllEnvs());

test("плитка «Сущности» показывает 300", async () => {
  await renderHome();
  const tile = screen.getByRole("group", { name: "Сущности" });
  expect(within(tile).getByText("300")).toBeTruthy();
  for (const name of ["Документы", "Связи", "Страницы wiki"]) {
    expect(screen.getByRole("group", { name })).toBeTruthy();
  }
});

test("числа в плитках — в русской записи", async () => {
  const { stats } = generateFixtures();
  await renderHome();
  const tile = screen.getByRole("group", { name: "Документы" });
  expect(tile.textContent).toContain(`${stats.chunks.toLocaleString("ru-RU")} фрагментов`);
});

test("полос по типам — 8, строка «Person» с числом из фикстуры", async () => {
  const { types } = generateFixtures();
  await renderHome();
  const bars = screen.getByRole("list", { name: "Сущности по типам" });
  expect(within(bars).getAllByRole("listitem")).toHaveLength(8);
  const person = within(bars).getByRole("link", { name: /^Person/ });
  expect(person.textContent).toContain(String(types.find((t) => t.type === "Person")!.count));
  expect(person.getAttribute("href")).toBe("/graph?types=Person");
});

test("самые важные сущности ведут на граф с подсветкой", async () => {
  const { graph } = generateFixtures();
  const top = [...graph.nodes].sort((a, b) => (b.pagerank ?? 0) - (a.pagerank ?? 0))[0]!;
  const router = await renderHome();
  const list = screen.getByRole("list", { name: "Самые важные сущности" });
  expect(within(list).getAllByRole("listitem")).toHaveLength(8);
  const escaped = top.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const link = within(list).getByRole("link", { name: new RegExp(`^${escaped}`) });
  expect(link.getAttribute("href")).toBe(`/graph?${new URLSearchParams({ highlight: top.name })}`);
  await act(async () => {
    fireEvent.click(link);
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/graph"));
  expect(router.state.location.search).toMatchObject({ highlight: top.name });
});

test("последние документы — 5, анализ графа готов", async () => {
  await renderHome();
  const docs = screen.getByRole("list", { name: "Последние документы" });
  expect(within(docs).getAllByRole("listitem")).toHaveLength(5);
  expect(screen.getByRole("heading", { name: "Анализ графа" })).toBeTruthy();
  expect(screen.getByText("Готов")).toBeTruthy();
});

test("без фикстур — спокойное сообщение", async () => {
  vi.stubEnv("VITE_FIXTURES", "");
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  expect(await screen.findByText(/API появится на этапе 2/, {}, { timeout: 3000 })).toBeTruthy();
});
