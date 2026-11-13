import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { createAppRouter } from "./router";

// Страница графа тянет sigma/three, а в jsdom нет WebGL.
vi.mock("./graph/Graph2D", async () => ({ Graph2D: (await import("./test/fakeGraph")).FakeGraph2D }));
vi.mock("./graph/Graph3D", async () => ({ default: (await import("./test/fakeGraph")).FakeGraph3D }));

test("приложение показывает бренд", async () => {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  await router.load();
  render(<App router={router} />);
  expect((await screen.findAllByText("mnogobase")).length).toBeGreaterThan(0);
});

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
