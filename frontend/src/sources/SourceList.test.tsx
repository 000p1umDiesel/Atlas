import { fireEvent, screen, within } from "@testing-library/react";
import type { Source } from "../api/types";
import { renderWithRouter } from "../test/router";
import { SourceList } from "./SourceList";

const CHUNK_ID = "0123456789abcdef:00003";

function src(over: Partial<Source>): Source {
  return { n: 1, kind: "chunk", ref: CHUNK_ID, path: null, page: null, snippet: "", cited: false, ...over };
}

const SOURCES: Source[] = [
  src({ n: 1, kind: "chunk", path: "отчёт.pdf", page: 4, snippet: "Начало фрагмента", cited: true }),
  src({ n: 2, kind: "wiki", ref: "e42#Раздел", path: "adam-smith.md", cited: true }),
  src({ n: 3, kind: "entity", ref: "e7", snippet: "Адам Смит\nэкономист", cited: false }),
  src({ n: 4, kind: "relation", ref: "r1", snippet: "Смит — автор «Богатства народов»", cited: false }),
  src({ n: 5, kind: "chunk", path: null, available: false, cited: false }),
];

test("цитированные — списком, остальные — в «Ещё N в контексте»", async () => {
  const { container } = await renderWithRouter(<SourceList sources={SOURCES} />);
  const lists = container.querySelectorAll("ol");
  expect(lists).toHaveLength(2);
  expect(within(lists[0] as HTMLElement).getAllByRole("listitem")).toHaveLength(2);
  const details = container.querySelector("details")!;
  expect(details.querySelector("summary")!.textContent).toBe("Ещё 3 в контексте, без ссылок в ответе");
  expect(within(details).getAllByRole("listitem")).toHaveLength(3);
});

test("подписи: номер, имя, вид, страница, «удалён»; подсказка — сниппет", async () => {
  await renderWithRouter(<SourceList sources={SOURCES} />);
  const first = screen.getByRole("button", { name: /отчёт\.pdf/ });
  expect(first.textContent).toBe("1отчёт.pdfдокумент, стр. 4");
  expect(first.getAttribute("title")).toBe("Начало фрагмента");
  expect(screen.getByRole("link", { name: /adam-smith/ }).textContent).toBe("2adam-smithwiki");
  expect(screen.getByRole("button", { name: /Адам Смит/ }).textContent).toBe("3Адам Смитсущность");
  expect(screen.getByRole("button", { name: /^5/ }).textContent).toBe("5документдокумент, удалён");
});

test("без цитированных — «Источники: N»; пустой список не рисуется", async () => {
  const { container, unmount } = await renderWithRouter(
    <SourceList sources={SOURCES.map((s) => ({ ...s, cited: false }))} />,
  );
  expect(container.querySelector("summary")!.textContent).toBe("Источники: 5");
  expect(container.querySelectorAll("ol")).toHaveLength(1);
  unmount();
  const empty = await renderWithRouter(<SourceList sources={[]} />);
  expect(empty.container.querySelector("ol, details")).toBeNull();
});

test("клик: фрагмент и сущность — в панель, wiki — переход, связь — без действия", async () => {
  const { router } = await renderWithRouter(<SourceList sources={SOURCES} />, "/chat/c1?x=1");
  fireEvent.click(screen.getByRole("button", { name: /отчёт\.pdf/ }));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ x: "1", panel: `chunk:${CHUNK_ID}` }));

  fireEvent.click(screen.getByRole("button", { name: /Адам Смит/ }));
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ x: "1", panel: "entity:e7" }));
  expect(router.state.location.pathname).toBe("/chat/c1");

  // связь — не кнопка и не ссылка: сниппет виден в строке
  expect(screen.queryByRole("button", { name: /Богатства/ })).toBeNull();
  expect(screen.queryByRole("link", { name: /Богатства/ })).toBeNull();
  expect(screen.getByText("Смит — автор «Богатства народов»", { selector: "li *" })).toBeTruthy();

  const wiki = screen.getByRole("link", { name: /adam-smith/ });
  expect(wiki.getAttribute("href")).toBe("/wiki/page/e42");
  expect(fireEvent.click(wiki)).toBe(false);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/wiki/page/e42"));
});
