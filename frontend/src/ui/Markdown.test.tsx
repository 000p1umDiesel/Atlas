import { fireEvent, render, screen } from "@testing-library/react";
import { renderWithRouter } from "../test/router";
import { Markdown } from "./Markdown";

test("клик по [2] вызывает onRef({kind: cite, n: 2})", () => {
  const onRef = vi.fn();
  render(<Markdown text="Ответ [2]." env={{ citations: true }} onRef={onRef} />);
  fireEvent.click(screen.getByRole("link", { name: "2" }));
  expect(onRef).toHaveBeenCalledWith({ kind: "cite", n: 2 });
});

test("клик по сноске вызывает onRef({kind: chunk, id})", () => {
  const onRef = vi.fn();
  const id = "0123456789abcdef:00007";
  render(<Markdown text={`Факт[^${id}]`} env={{ footnotes: new Map([[id, 1]]) }} onRef={onRef} />);
  fireEvent.click(screen.getByRole("link", { name: "1" }));
  expect(onRef).toHaveBeenCalledWith({ kind: "chunk", id });
});

test("по умолчанию класс prose, можно задать свой", () => {
  const { container, rerender } = render(<Markdown text="x" />);
  expect((container.firstChild as HTMLElement).className).toBe("prose");
  rerender(<Markdown text="x" className="prose small" />);
  expect((container.firstChild as HTMLElement).className).toBe("prose small");
});

test("внутренние ссылки — переход роутера без перезагрузки, внешние — как есть", async () => {
  const { router } = await renderWithRouter(
    <Markdown text="[[adam-smith|Адам]] [док](/documents/d1/read?chunk=2) [внеш](https://ex.org)" />,
  );
  const wiki = screen.getByRole("link", { name: "Адам" });
  // fireEvent.click возвращает false, если обработчик отменил действие по умолчанию
  expect(fireEvent.click(wiki)).toBe(false);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/wiki/adam-smith"));

  expect(fireEvent.click(screen.getByRole("link", { name: "док" }))).toBe(false);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/documents/d1/read"));
  expect(router.state.location.search).toEqual({ chunk: "2" });

  const external = screen.getByRole("link", { name: "внеш" });
  external.addEventListener("click", (event) => event.preventDefault()); // jsdom не уходит со страницы
  fireEvent.click(external);
  expect(router.state.location.pathname).toBe("/documents/d1/read");
});

test("клик с Ctrl по внутренней ссылке — браузеру (новая вкладка)", async () => {
  const { router } = await renderWithRouter(<Markdown text="[[adam-smith|Адам]]" />);
  const wiki = screen.getByRole("link", { name: "Адам" });
  wiki.addEventListener("click", (event) => event.preventDefault());
  fireEvent.click(wiki, { ctrlKey: true });
  expect(router.state.location.pathname).toBe("/chat");
});
