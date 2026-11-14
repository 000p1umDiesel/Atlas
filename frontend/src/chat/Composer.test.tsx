import { act, fireEvent, render, screen } from "@testing-library/react";
import { Composer } from "./Composer";

const box = () => screen.getByRole("textbox", { name: "Вопрос" }) as HTMLTextAreaElement;
const ask = () => screen.getByRole("button", { name: "Спросить" }) as HTMLButtonElement;
const radio = (name: string) => screen.getByRole("radio", { name }) as HTMLInputElement;

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

test("Enter отправляет вопрос и режим; поле очищается, только когда вопрос принят", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />);
  fireEvent.change(box(), { target: { value: "  Кто такой Смит?  " } });
  fireEvent.keyDown(box(), { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("Кто такой Смит?", "rag", expect.any(Function));
  // Ошибка до начала ответа не должна стереть вопрос.
  expect(box().value).toBe("  Кто такой Смит?  ");
  act(() => onSend.mock.calls[0]![2]());
  expect(box().value).toBe("");
});

test("начальный текст поля — вопрос, который сервер не принял", () => {
  render(<Composer onSend={vi.fn()} initialText="Неотправленный" />);
  expect(box().value).toBe("Неотправленный");
});

test("Shift+Enter и Enter во время IME-набора не отправляют", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />);
  fireEvent.change(box(), { target: { value: "вопрос" } });
  fireEvent.keyDown(box(), { key: "Enter", shiftKey: true });
  fireEvent.keyDown(box(), { key: "Enter", isComposing: true });
  fireEvent.keyDown(box(), { key: "Enter", keyCode: 229 });
  expect(onSend).not.toHaveBeenCalled();
  expect(box().value).toBe("вопрос");
});

test("пустой или пробельный вопрос — «Спросить» недоступна и Enter ничего не шлёт", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />);
  expect(ask().disabled).toBe(true);
  fireEvent.change(box(), { target: { value: "   \n " } });
  expect(ask().disabled).toBe(true);
  fireEvent.keyDown(box(), { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.change(box(), { target: { value: "вопрос" } });
  expect(ask().disabled).toBe(false);
  fireEvent.click(ask());
  expect(onSend).toHaveBeenCalledWith("вопрос", "rag", expect.any(Function));
});

test("во время ответа — «Отвечаю…», кнопка и режимы недоступны", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} busy />);
  fireEvent.change(box(), { target: { value: "вопрос" } });
  const button = screen.getByRole("button", { name: "Отвечаю…" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(radio("Граф").disabled).toBe(true);
  fireEvent.keyDown(box(), { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
});

test("режимы — четыре, с подсказками; по умолчанию «Фрагменты»", () => {
  render(<Composer onSend={vi.fn()} />);
  expect(screen.getByRole("radiogroup", { name: "Где искать ответ" })).toBeTruthy();
  expect(screen.getAllByRole("radio").map((r) => r.closest("label")!.textContent)).toEqual([
    "Фрагменты",
    "Wiki",
    "Граф",
    "Всё вместе",
  ]);
  expect(radio("Граф").closest("label")!.getAttribute("title")).toBe("по связям сущностей");
  expect(radio("Фрагменты").checked).toBe(true);
});

test("выбор режима сохраняется в mb.mode и восстанавливается после перемонтирования", () => {
  const onSend = vi.fn();
  const first = render(<Composer onSend={onSend} />);
  fireEvent.click(radio("Граф"));
  expect(localStorage.getItem("mb.mode")).toBe("graph");
  first.unmount();
  render(<Composer onSend={onSend} />);
  expect(radio("Граф").checked).toBe(true);
  fireEvent.change(box(), { target: { value: "вопрос" } });
  fireEvent.keyDown(box(), { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("вопрос", "graph", expect.any(Function));
});

test("мусор в mb.mode — «Фрагменты»", () => {
  localStorage.setItem("mb.mode", "everything");
  render(<Composer onSend={vi.fn()} />);
  expect(radio("Фрагменты").checked).toBe(true);
});

test("недоступный localStorage — «Фрагменты» без ошибки, выбор всё равно работает", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("SecurityError");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("SecurityError");
  });
  render(<Composer onSend={vi.fn()} />);
  expect(radio("Фрагменты").checked).toBe(true);
  act(() => {
    fireEvent.click(radio("Wiki"));
  });
  expect(radio("Wiki").checked).toBe(true);
});

test("«Сравнить режимы»: нет выбора режима, свой плейсхолдер и кнопка; вопрос уходит в onCompare", () => {
  const onSend = vi.fn();
  const onCompare = vi.fn();
  render(<Composer onSend={onSend} onCompare={onCompare} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "Сравнить режимы" }));
  expect(screen.queryByRole("radiogroup")).toBeNull();
  expect(box().placeholder).toBe("Вопрос для сравнения режимов…");
  fireEvent.change(box(), { target: { value: " Вопрос " } });
  fireEvent.click(screen.getByRole("button", { name: "Сравнить" }));
  expect(onCompare).toHaveBeenCalledWith("Вопрос");
  expect(onSend).not.toHaveBeenCalled();
});
