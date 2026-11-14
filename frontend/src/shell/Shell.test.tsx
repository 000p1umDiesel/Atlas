import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
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
  expect(await screen.findAllByRole("heading", { name: "О чём спросить?" })).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "Граф" })).toBeNull();
});

test("переход внутри раздела не перемонтирует обёртку страницы, в другой раздел — перемонтирует", async () => {
  const router = await renderAt("/chat");
  await screen.findByRole("heading", { name: "О чём спросить?" });
  const wrapper = () => screen.getByRole("main").firstElementChild;
  const first = wrapper();
  await act(() => router.navigate({ to: "/chat/$id", params: { id: "c1" } }));
  expect(router.state.location.pathname).toBe("/chat/c1");
  expect(wrapper()).toBe(first);
  await act(() => router.navigate({ to: "/chat/$id", params: { id: "c2" } }));
  expect(wrapper()).toBe(first);
  await act(() => router.navigate({ to: "/status" }));
  await screen.findByRole("heading", { name: "Статус" });
  expect(wrapper()).not.toBe(first);
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
  // Затемнение под меню — токен --scrim, не --ink (тот в тёмной теме светлый).
  expect(document.querySelector("[data-menu-backdrop]")?.classList.contains("bg-scrim")).toBe(true);
  fireEvent.click(within(menu).getByRole("switch", { name: "Скрыть одиночные" }));
  expect(screen.getByRole("complementary", { name: "Меню раздела" })).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  await vi.waitFor(() => expect(screen.queryByRole("complementary", { name: "Меню раздела" })).toBeNull());
});

test("на страницах без колонки её нет", async () => {
  await renderAt("/status");
  await screen.findByRole("heading", { name: "Статус" });
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

test("на телефоне тема переключается из верхней панели (кнопка не скрыта без md:)", async () => {
  await renderAt("/");
  const bar = (await screen.findByText("mnogobase", { selector: "header span" })).closest("header")!;
  const mobile = within(bar).getByRole("button", { name: /^Тема:/ });
  expect(mobile.className.split(/\s+/)).not.toContain("hidden");
  expect(mobile.className.split(/\s+/)).toContain("md:hidden");
  const before = mobile.getAttribute("aria-label");
  fireEvent.click(mobile);
  const after = within(bar).getByRole("button", { name: /^Тема:/ }).getAttribute("aria-label");
  expect(after).not.toBe(before);
  // рейка (на широком экране) показывает тот же выбор
  const nav = screen.getByRole("navigation", { name: "Разделы" });
  expect(within(nav).getByRole("button", { name: /^Тема:/ }).getAttribute("aria-label")).toBe(after);
});

test("на телефоне «Аккаунт» и «Выйти» — в меню верхней панели", async () => {
  await renderAt("/chat");
  const bar = (await screen.findByText("mnogobase", { selector: "header span" })).closest("header")!;
  const toggle = within(bar).getByRole("button", { name: "Меню аккаунта" });
  expect(toggle.className.split(/\s+/)).toContain("md:hidden");
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(within(bar).queryByRole("button", { name: "Выйти" })).toBeNull();
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(within(bar).getByRole("link", { name: "Аккаунт" }).getAttribute("href")).toBe("/account");
  expect(within(bar).getByRole("button", { name: "Выйти" })).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  await vi.waitFor(() => expect(within(bar).queryByRole("button", { name: "Выйти" })).toBeNull());
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
});

test("новый путь открывает страницу с начала; смена только search-параметров прокрутку не сбрасывает", async () => {
  const router = await renderAt("/status");
  await screen.findByRole("heading", { name: "Статус" });
  const main = screen.getByRole("main");
  main.scrollTop = 500;
  // правая панель — только search-параметр: прокрутка остаётся
  await act(() => router.navigate({ to: "/status", search: { panel: "entity:e1" } as never }));
  expect(router.state.location.search).toEqual({ panel: "entity:e1" });
  expect(main.scrollTop).toBe(500);
  await act(() => router.navigate({ to: "/account" }));
  await screen.findByRole("heading", { name: "Аккаунт" });
  expect(screen.getByRole("main")).toBe(main);
  expect(main.scrollTop).toBe(0);
});

test("неизвестный путь — русская страница «Страница не найдена» в оболочке", async () => {
  await renderAt("/no/such/page");
  expect(await screen.findByRole("heading", { name: "Страница не найдена" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "На главную" }).getAttribute("href")).toBe("/");
  expect(screen.getByRole("navigation", { name: "Разделы" })).toBeTruthy();
});

test("Ctrl+K открывает палитру на страницах оболочки, кнопка «Поиск» — тоже; на /login палитры нет", async () => {
  const router = await renderAt("/status");
  await screen.findByRole("heading", { name: "Статус" });
  fireEvent.keyDown(document, { key: "k", code: "KeyK", ctrlKey: true });
  expect(await screen.findByRole("dialog", { name: "Поиск" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Поиск" }), { key: "Escape" });
  await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: "Поиск" })).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Поиск" }));
  expect(await screen.findByRole("dialog", { name: "Поиск" })).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: "Поиск" })).toBeNull());
  await act(() => router.navigate({ to: "/login" }));
  await screen.findByRole("button", { name: "Войти" });
  fireEvent.keyDown(document, { key: "k", code: "KeyK", ctrlKey: true });
  await act(async () => new Promise((r) => setTimeout(r, 50)));
  expect(screen.queryByRole("dialog", { name: "Поиск" })).toBeNull();
});
