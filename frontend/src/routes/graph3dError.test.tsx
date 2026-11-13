import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
// Чанк 3D-вида прежней сборки удалён с сервера: ленивый импорт падает.
vi.mock("../graph/Graph3D", () => ({
  default: () => {
    throw new TypeError("Failed to fetch dynamically imported module: https://kb/assets/Graph3D-old.js");
  },
}));

beforeAll(() => {
  window.scrollTo = vi.fn();
});

beforeEach(() => vi.stubEnv("VITE_FIXTURES", "1"));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

test("3D-чанк не загрузился → «Сайт обновился» в области графа, фильтры живы", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/graph?view=3d"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("Сайт обновился — перезагрузите страницу");
  expect(within(alert).getByRole("button", { name: "Перезагрузить" })).toBeTruthy();
  expect(within(screen.getByRole("main")).getByRole("radio", { name: "2D" })).toBeTruthy();
  expect(screen.getByRole("complementary", { name: "Контекст раздела" })).toBeTruthy();
});
