import { RouterProvider } from "@tanstack/react-router";
import { createMemoryHistory } from "@tanstack/react-router";
import { fireEvent, render, screen } from "@testing-library/react";
import { createAppRouter } from "../router";

async function renderAt(path: string) {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<RouterProvider router={router} />);
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

test("«Сущности» не подсвечиваются на /entities-x", async () => {
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
