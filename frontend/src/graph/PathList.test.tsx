import { fireEvent, render, screen } from "@testing-library/react";
import type { PathView } from "../api/types";
import { PathList } from "./PathList";

const node = (id: string) => ({
  entity_id: id,
  name: id,
  type: "Person",
  mention_count: 1,
  pagerank: null,
  community: null,
  degree: 1,
});
const relation = (src: string, dst: string, predicate = "predicate") => ({
  src_id: src,
  src_name: src,
  predicate,
  dst_id: dst,
  dst_name: dst,
  description: "",
  weight: 1,
  evidence: [],
  src_type: "Person",
  dst_type: "Person",
});

function renderList(paths: PathView[], extra: Partial<Parameters<typeof PathList>[0]> = {}) {
  const props = {
    paths,
    timedOut: false,
    hops: 4,
    pinned: null,
    onPin: vi.fn(),
    onHover: vi.fn(),
    onWiden: vi.fn(),
    ...extra,
  };
  render(<PathList {...props} />);
  return props;
}

test("связь b→a в пути a…b показывается как «a ← predicate ← b»", () => {
  renderList([{ nodes: [node("a"), node("b")], relations: [relation("b", "a")] }]);
  const item = screen.getByRole("button", { name: /Путь 1/ });
  expect(item.textContent).toContain("Путь 1 · 1 шаг");
  expect(item.textContent).toContain("a ← predicate ← b");
});

test("цепочка из нескольких шагов: стрелка у каждой связи — по её хранимому направлению", () => {
  renderList([
    { nodes: [node("a"), node("b"), node("c")], relations: [relation("a", "b", "p1"), relation("c", "b", "p2")] },
  ]);
  const item = screen.getByRole("button", { name: /Путь 1/ });
  expect(item.textContent).toContain("Путь 1 · 2 шага");
  expect(item.textContent).toContain("a → p1 → b ← p2 ← c");
});

test("предикат в цепочке — для людей: «proposed_by» → «proposed by»", () => {
  renderList([{ nodes: [node("a"), node("b")], relations: [relation("a", "b", "proposed_by")] }]);
  const item = screen.getByRole("button", { name: /Путь 1/ });
  expect(item.textContent).toContain("a → proposed by → b");
  expect(item.textContent).not.toContain("proposed_by");
});

test("наведение и фокус сообщают путь, клик закрепляет, повторный клик — снимает", () => {
  const paths = [
    { nodes: [node("a"), node("b")], relations: [relation("a", "b")] },
    { nodes: [node("a"), node("c"), node("b")], relations: [relation("a", "c"), relation("c", "b")] },
  ];
  const props = renderList(paths, { pinned: 1 });
  const second = screen.getByRole("button", { name: /Путь 2/ });
  expect(second.getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: /Путь 1/ }).getAttribute("aria-pressed")).toBe("false");
  fireEvent.mouseEnter(screen.getByRole("button", { name: /Путь 1/ }));
  expect(props.onHover).toHaveBeenLastCalledWith(0);
  fireEvent.mouseLeave(screen.getByRole("button", { name: /Путь 1/ }));
  expect(props.onHover).toHaveBeenLastCalledWith(null);
  fireEvent.focus(second);
  expect(props.onHover).toHaveBeenLastCalledWith(1);
  fireEvent.click(second);
  expect(props.onPin).toHaveBeenLastCalledWith(null);
  fireEvent.click(screen.getByRole("button", { name: /Путь 1/ }));
  expect(props.onPin).toHaveBeenLastCalledWith(0);
});

test("пусто: таймаут → совет уменьшить длину; иначе — «не нашлось» и расширение до 5 шагов", () => {
  const { unmount } = render(
    <PathList paths={[]} timedOut hops={4} pinned={null} onPin={vi.fn()} onHover={vi.fn()} onWiden={vi.fn()} />,
  );
  expect(screen.getByText("Поиск пути занял слишком долго. Уменьшите длину пути.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Искать до 5 шагов" })).toBeNull();
  unmount();

  const props = renderList([], { hops: 1 });
  expect(screen.getByText("Путей длиной до 1 шага не нашлось.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Искать до 5 шагов" }));
  expect(props.onWiden).toHaveBeenCalled();
});

test("при длине 5 расширять некуда", () => {
  renderList([], { hops: 5 });
  expect(screen.getByText("Путей длиной до 5 шагов не нашлось.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Искать до 5 шагов" })).toBeNull();
});
