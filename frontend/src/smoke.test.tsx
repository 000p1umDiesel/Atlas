import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { createAppRouter } from "./router";

test("приложение показывает бренд", async () => {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  await router.load();
  render(<App router={router} />);
  expect((await screen.findAllByText("mnogobase")).length).toBeGreaterThan(0);
});

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
