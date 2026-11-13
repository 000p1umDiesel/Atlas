import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryHistory } from "@tanstack/react-router";
import { createAppRouter } from "../router";
import { ErrorBoundary, ErrorNotice, isChunkLoadError } from "./ErrorNotice";

afterEach(() => vi.restoreAllMocks());

describe("isChunkLoadError", () => {
  it("узнаёт ошибки загрузки чанка всех браузеров и Vite", () => {
    for (const message of [
      "Failed to fetch dynamically imported module: https://kb/assets/Graph3D-abc.js",
      "Importing a module script failed.",
      "error loading dynamically imported module: https://kb/assets/graph-1.js",
      "Unable to preload CSS for /assets/graph-1.css",
    ])
      expect(isChunkLoadError(new TypeError(message))).toBe(true);
  });
  it("прочие ошибки — нет", () => {
    expect(isChunkLoadError(new Error("boom"))).toBe(false);
    expect(isChunkLoadError("Failed to fetch dynamically imported module")).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
  });
});

describe("ErrorNotice", () => {
  it("чанк после обновления сайта → просьба перезагрузить и кнопка", () => {
    const reload = vi.fn();
    render(<ErrorNotice error={new TypeError("Importing a module script failed.")} reload={reload} />);
    expect(screen.getByRole("alert").textContent).toContain("Сайт обновился — перезагрузите страницу");
    fireEvent.click(screen.getByRole("button", { name: "Перезагрузить" }));
    expect(reload).toHaveBeenCalledOnce();
  });
  it("прочая ошибка → общий русский текст без подробностей", () => {
    render(<ErrorNotice error={new Error("secret stack")} reload={() => {}} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Что-то пошло не так");
    expect(alert.textContent).not.toContain("secret");
    expect(screen.getByRole("button", { name: "Перезагрузить" })).toBeTruthy();
  });
});

test("граница ошибок показывает ErrorNotice вместо упавшего потомка", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  function Broken(): never {
    throw new TypeError("Failed to fetch dynamically imported module: /assets/Graph3D-x.js");
  }
  render(
    <ErrorBoundary>
      <Broken />
    </ErrorBoundary>,
  );
  expect(screen.getByRole("alert").textContent).toContain("Сайт обновился — перезагрузите страницу");
});

test("роутер по умолчанию показывает русскую ошибку", () => {
  const router = createAppRouter({ history: createMemoryHistory() });
  expect(router.options.defaultErrorComponent).toBeDefined();
  const Component = router.options.defaultErrorComponent!;
  render(<Component error={new Error("x")} reset={() => {}} />);
  expect(screen.getByRole("alert").textContent).toContain("Что-то пошло не так");
});
