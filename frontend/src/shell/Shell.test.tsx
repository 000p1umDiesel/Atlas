import { createMemoryHistory } from "@tanstack/react-router";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";

// Страница графа тянет sigma/three, а в jsdom нет WebGL.
vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
vi.mock("../graph/Graph3D", async () => ({ default: (await import("../test/fakeGraph")).FakeGraph3D }));

async function renderAt(path: string) {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  return router;
}

test("на /graph пункт «Граф» активен", async () => {
  await renderAt("/graph");
  const link = await screen.findByRole("link", { name: "Граф" });
  expect(link.getAttribute("aria-current")).toBe("page");
  expect(screen.getByRole("link", { name: "Чат" }).getAttribute("aria-current")).toBeNull();
});

test("клик по «Главная» меняет путь на /", async () => {
  const router = await renderAt("/graph");
  fireEvent.click(await screen.findByRole("link", { name: "Главная" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/"));
});

test("после перехода в DOM только новая страница", async () => {
  const router = await renderAt("/graph");
  await screen.findByRole("heading", { name: "Граф" });
  fireEvent.click(screen.getByRole("link", { name: "Чат" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
  expect(await screen.findAllByRole("heading", { name: "Чат" })).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "Граф" })).toBeNull();
});

test("на /chat активен только «Чат»", async () => {
  await renderAt("/chat");
  expect((await screen.findByRole("link", { name: "Чат" })).getAttribute("aria-current")).toBe("page");
});

test("оболочка показывает бренд", async () => {
  await renderAt("/");
  expect((await screen.findAllByText("mnogobase")).length).toBeGreaterThan(0);
});

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});

test("на графе есть колонка фильтров, её можно свернуть", async () => {
  await renderAt("/graph");
  const column = await screen.findByRole("complementary", { name: "Контекст раздела" });
  expect(within(column).getByRole("heading", { name: "Фильтры" })).toBeTruthy();
  fireEvent.click(within(column).getByRole("button", { name: "Свернуть фильтры" }));
  await vi.waitFor(() => expect(screen.queryByRole("complementary", { name: "Контекст раздела" })).toBeNull());
});

test("мобильное меню раздела закрывается по Escape, клик по фильтру его не закрывает", async () => {
  await renderAt("/graph");
  fireEvent.click(await screen.findByRole("button", { name: "Меню раздела" }));
  const menu = await screen.findByRole("complementary", { name: "Меню раздела" });
  fireEvent.click(within(menu).getByRole("switch", { name: "Скрыть одиночные" }));
  expect(screen.getByRole("complementary", { name: "Меню раздела" })).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  await vi.waitFor(() => expect(screen.queryByRole("complementary", { name: "Меню раздела" })).toBeNull());
});

test("на страницах без колонки её нет", async () => {
  await renderAt("/chat");
  await screen.findByRole("heading", { name: "Чат" });
  expect(screen.queryByRole("complementary", { name: "Контекст раздела" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Меню раздела" })).toBeNull();
});

test("тема на /_design и кнопка рейки — одно состояние", async () => {
  await renderAt("/_design");
  const group = await screen.findByRole("group", { name: "Тема" });
  fireEvent.click(within(group).getByRole("button", { name: "Тёмная" }));
  const nav = screen.getByRole("navigation", { name: "Разделы" });
  expect(within(nav).getByRole("button", { name: "Тема: тёмная" })).toBeTruthy();
  expect(document.documentElement.dataset.theme).toBe("dark");
  // и обратно: кнопка рейки переключает выбор на странице
  fireEvent.click(within(nav).getByRole("button", { name: "Тема: тёмная" }));
  expect(within(group).getByRole("button", { name: "Как в системе" }).getAttribute("aria-pressed")).toBe("true");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});
