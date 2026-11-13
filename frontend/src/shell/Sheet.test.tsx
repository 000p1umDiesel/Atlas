import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { Sheet } from "./Sheet";

function Host({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>открыть</button>
      <Sheet
        open={open}
        title="Детали"
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
      >
        <p>содержимое</p>
      </Sheet>
    </>
  );
}

test("открытая панель — немодальный dialog, фокус внутри", async () => {
  render(<Host />);
  fireEvent.click(screen.getByText("открыть"));
  const dialog = await screen.findByRole("dialog", { name: "Детали" });
  expect(dialog.getAttribute("aria-modal")).toBe("false");
  expect(dialog.contains(document.activeElement)).toBe(true);
});

test("Escape закрывает и возвращает фокус открывателю", async () => {
  const onClose = vi.fn();
  render(<Host onClose={onClose} />);
  const opener = screen.getByText("открыть");
  opener.focus();
  fireEvent.click(opener);
  await screen.findByRole("dialog");
  fireEvent.keyDown(document, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
  await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.activeElement).toBe(opener);
});

test("кнопка закрытия вызывает onClose", async () => {
  const onClose = vi.fn();
  render(<Host onClose={onClose} />);
  fireEvent.click(screen.getByText("открыть"));
  fireEvent.click(await screen.findByRole("button", { name: "Закрыть" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
