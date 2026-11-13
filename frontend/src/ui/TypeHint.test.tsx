import { act, fireEvent, render, screen } from "@testing-library/react";
import { TypeHint } from "./TypeHint";

const info = { type: "Person", label: "Человек", description: "Реальный или вымышленный человек.", known: true };

function Chip({ toggleOnClick = false }: { toggleOnClick?: boolean }) {
  return (
    <TypeHint info={info} color="var(--t0)" toggleOnClick={toggleOnClick}>
      {(trigger) => (
        <button type="button" {...trigger}>
          Человек
        </button>
      )}
    </TypeHint>
  );
}

afterEach(() => vi.useRealTimers());

test("определение связано с кнопкой через aria-describedby и скрыто, пока нет наведения", () => {
  render(<Chip />);
  const button = screen.getByRole("button", { name: "Человек" });
  const id = button.getAttribute("aria-describedby");
  expect(id).toBeTruthy();
  expect(document.getElementById(id!)?.textContent).toContain("Реальный или вымышленный человек.");
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("наведение мышью показывает подсказку с задержкой, уход — прячет", () => {
  vi.useFakeTimers();
  render(<Chip />);
  const button = screen.getByRole("button", { name: "Человек" });
  fireEvent.pointerEnter(button, { pointerType: "mouse" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  act(() => vi.advanceTimersByTime(500));
  const tip = screen.getByRole("tooltip");
  expect(tip.textContent).toContain("Человек");
  expect(tip.textContent).toContain("Реальный или вымышленный человек.");
  fireEvent.pointerLeave(button, { pointerType: "mouse" });
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("касание пальцем подсказку не открывает", () => {
  vi.useFakeTimers();
  render(<Chip />);
  fireEvent.pointerEnter(screen.getByRole("button", { name: "Человек" }), { pointerType: "touch" });
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("фокус с клавиатуры показывает подсказку сразу, Esc и уход фокуса прячут", () => {
  render(<Chip />);
  const button = screen.getByRole("button", { name: "Человек" });
  act(() => button.focus());
  expect(screen.getByRole("tooltip").textContent).toContain("Реальный");
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  act(() => button.blur());
  act(() => button.focus());
  expect(screen.getByRole("tooltip")).toBeTruthy();
  act(() => button.blur());
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("toggleOnClick: нажатие открывает и закрывает (для касаний)", () => {
  render(<Chip toggleOnClick />);
  const button = screen.getByRole("button", { name: "Человек" });
  fireEvent.click(button);
  expect(screen.getByRole("tooltip")).toBeTruthy();
  fireEvent.click(button);
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("без определения подсказки нет, кнопка — как есть", () => {
  render(
    <TypeHint info={{ ...info, description: "", known: false }} color="var(--t0)">
      {(trigger) => (
        <button type="button" {...trigger}>
          X
        </button>
      )}
    </TypeHint>,
  );
  expect(screen.getByRole("button", { name: "X" }).hasAttribute("aria-describedby")).toBe(false);
});
