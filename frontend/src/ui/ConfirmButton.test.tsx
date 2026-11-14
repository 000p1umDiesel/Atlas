import { act, fireEvent, render, screen } from "@testing-library/react";
import { ConfirmButton } from "./ConfirmButton";

function setup(busy = false) {
  const onConfirm = vi.fn();
  const view = render(
    <div>
      <ConfirmButton label="Удалить" confirmLabel="Удалить навсегда" onConfirm={onConfirm} busy={busy} />
      <button type="button">снаружи</button>
    </div>,
  );
  return { onConfirm, ...view };
}

test("первый клик не вызывает onConfirm, второй — вызывает", () => {
  const { onConfirm } = setup();
  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  expect(onConfirm).not.toHaveBeenCalled();
  const confirm = screen.getByRole("button", { name: "Удалить навсегда" });
  expect(screen.getByRole("button", { name: "Отмена" })).toBeTruthy();
  expect(document.activeElement).toBe(confirm);
  fireEvent.click(confirm);
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

test("«Отмена», Esc и клик снаружи возвращают исходную кнопку", () => {
  const { onConfirm } = setup();
  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
  expect(screen.queryByRole("button", { name: "Удалить навсегда" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Удалить" }));

  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  fireEvent.keyDown(screen.getByRole("button", { name: "Удалить навсегда" }), { key: "Escape" });
  expect(screen.queryByRole("button", { name: "Удалить навсегда" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  fireEvent.pointerDown(screen.getByRole("button", { name: "снаружи" }));
  expect(screen.queryByRole("button", { name: "Удалить навсегда" })).toBeNull();
  expect(onConfirm).not.toHaveBeenCalled();
});

test("уход фокуса клавишей Tab за пределы отменяет", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  const outside = screen.getByRole("button", { name: "снаружи" });
  act(() => outside.focus());
  expect(screen.queryByRole("button", { name: "Удалить навсегда" })).toBeNull();
});

test("busy — кнопки недоступны", () => {
  const { onConfirm } = setup(true);
  const button = screen.getByRole("button", { name: "Удалить" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  fireEvent.click(button);
  expect(onConfirm).not.toHaveBeenCalled();
});
