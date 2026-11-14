import { act, fireEvent, render, screen } from "@testing-library/react";
import type { Job } from "../api/types";
import { inFlight, installFakeXhr, type FakeXhr } from "../test/fakeXhr";
import { Uploader } from "./Uploader";

const job = (over: Partial<Job> = {}): Job => ({
  id: 1,
  kind: "ingest",
  status: "pending",
  attempts: 0,
  available_at: 1_800_000_000,
  error: null,
  progress: "",
  created_at: 1_800_000_000,
  updated_at: 1_800_000_000,
  doc_id: null,
  name: "a.pdf",
  stage: null,
  step_done: null,
  step_total: null,
  ...over,
});

const file = (name: string) => new File(["x"], name);
const input = () => document.querySelector<HTMLInputElement>('input[type="file"]')!;
const zone = () => screen.getByText("Перетащите файлы сюда").closest("[data-dropzone]") as HTMLElement;
const row = (name: string) => screen.getByText(name).closest("li") as HTMLElement;

/** Ответ на запрос и обработка обещаний (переход к следующему файлу). */
async function respond(xhr: FakeXhr, status: number, body: unknown) {
  await act(async () => {
    xhr.respond(status, body);
    await new Promise((r) => setTimeout(r, 0));
  });
}

afterEach(() => vi.unstubAllGlobals());

test("тексты зоны, скрытый выбор файлов, подсветка при перетаскивании", () => {
  installFakeXhr();
  render(<Uploader onUploaded={() => {}} />);
  expect(screen.getByRole("button", { name: "выберите на диске" })).toBeTruthy();
  expect(
    screen.getByText(
      "PDF, Word, PowerPoint, Markdown, HTML, картинки и другие форматы из настроек сервера. Файл появится в поиске, когда обработка закончится.",
    ),
  ).toBeTruthy();
  expect(input().multiple).toBe(true);
  expect(input().hidden).toBe(true);

  const click = vi.spyOn(input(), "click");
  fireEvent.click(screen.getByRole("button", { name: "выберите на диске" }));
  expect(click).toHaveBeenCalled();

  fireEvent.dragOver(zone(), { dataTransfer: { types: ["Files"] } });
  expect(zone().dataset.over).toBe("true");
  fireEvent.dragLeave(zone());
  expect(zone().dataset.over).toBeUndefined();
  // перетаскивают текст, а не файлы — зона не подсвечивается
  fireEvent.dragOver(zone(), { dataTransfer: { types: ["text/plain"] } });
  expect(zone().dataset.over).toBeUndefined();
});

test.each(["abort", "timeout"] as const)("%s у файла не останавливает очередь", async (how) => {
  const xhrs = installFakeXhr();
  render(<Uploader onUploaded={() => {}} />);
  await act(async () => {
    fireEvent.change(input(), { target: { files: [file("a.pdf"), file("b.pdf")] } });
  });
  await act(async () => {
    xhrs[0]![how]();
    await new Promise((r) => setTimeout(r, 0));
  });
  expect(row("a.pdf").textContent).toContain("Нет связи с сервером");
  expect(xhrs).toHaveLength(2);
  expect(xhrs[1]!.url).toContain("filename=b.pdf");
  await respond(xhrs[1]!, 202, { ...job(), duplicate: false });
  expect(row("b.pdf").textContent).toContain("принят");
});

test("три файла по одному: ошибка второго не останавливает очередь, дубликат — «уже в базе»", async () => {
  const xhrs = installFakeXhr();
  const onUploaded = vi.fn();
  render(<Uploader onUploaded={onUploaded} />);

  await act(async () => {
    fireEvent.change(input(), { target: { files: [file("a.pdf"), file("b.exe"), file("c.pdf")] } });
  });
  expect(input().value).toBe(""); // выбор сброшен: тот же файл можно выбрать снова

  // строго по одному
  expect(xhrs).toHaveLength(1);
  expect(inFlight()).toHaveLength(1);
  expect(xhrs[0]!.url).toContain("filename=a.pdf");
  expect(row("b.exe").textContent).toContain("ждёт");
  expect(row("c.pdf").textContent).toContain("ждёт");

  act(() => xhrs[0]!.progress(42, 100));
  expect(row("a.pdf").textContent).toContain("42 %");
  const bar = screen.getByRole("progressbar", { name: "Загрузка a.pdf" }) as HTMLProgressElement;
  expect(bar.max).toBe(1);
  expect(bar.value).toBeCloseTo(0.42);

  await respond(xhrs[0]!, 202, { ...job(), duplicate: false });
  expect(row("a.pdf").textContent).toContain("принят");
  expect(onUploaded).toHaveBeenCalledTimes(1);
  expect(xhrs).toHaveLength(2);
  expect(inFlight()).toHaveLength(1);
  expect(xhrs[1]!.url).toContain("filename=b.exe");

  await respond(xhrs[1]!, 422, { detail: "Неподдерживаемый тип документа" });
  expect(row("b.exe").textContent).toContain("Неподдерживаемый тип документа");
  expect(onUploaded).toHaveBeenCalledTimes(1);
  expect(xhrs).toHaveLength(3);
  expect(xhrs[2]!.url).toContain("filename=c.pdf");

  await respond(xhrs[2]!, 202, { ...job({ status: "done" }), duplicate: true });
  expect(row("c.pdf").textContent).toContain("уже в базе");
  expect(onUploaded).toHaveBeenCalledTimes(2);
  expect(xhrs).toHaveLength(3);
});

test("дубликат ещё не обработанного файла — «уже в очереди»", async () => {
  const xhrs = installFakeXhr();
  render(<Uploader onUploaded={() => {}} />);
  await act(async () => {
    fireEvent.drop(zone(), { dataTransfer: { files: [file("a.pdf")] } });
  });
  await respond(xhrs[0]!, 202, { ...job({ status: "running" }), duplicate: true });
  expect(row("a.pdf").textContent).toContain("уже в очереди");
});

test("файлы, добавленные во время загрузки, ждут своей очереди", async () => {
  const xhrs = installFakeXhr();
  render(<Uploader onUploaded={() => {}} />);
  await act(async () => {
    fireEvent.change(input(), { target: { files: [file("a.pdf")] } });
  });
  await act(async () => {
    fireEvent.drop(zone(), { dataTransfer: { files: [file("b.pdf")] } });
  });
  expect(xhrs).toHaveLength(1);
  await respond(xhrs[0]!, 202, { ...job(), duplicate: false });
  expect(xhrs).toHaveLength(2);
  expect(xhrs[1]!.url).toContain("filename=b.pdf");
});

test("«Очистить список» оставляет ждущие и текущий", async () => {
  const xhrs = installFakeXhr();
  render(<Uploader onUploaded={() => {}} />);
  await act(async () => {
    fireEvent.change(input(), { target: { files: [file("a.pdf"), file("b.pdf"), file("c.pdf"), file("d.pdf")] } });
  });
  await respond(xhrs[0]!, 202, { ...job(), duplicate: false });
  await respond(xhrs[1]!, 413, { detail: "Загрузка слишком большая" });
  // a — принят, b — ошибка, c — грузится, d — ждёт
  fireEvent.click(screen.getByRole("button", { name: "Очистить список" }));
  expect(screen.queryByText("a.pdf")).toBeNull();
  expect(screen.queryByText("b.pdf")).toBeNull();
  expect(screen.getByText("c.pdf")).toBeTruthy();
  expect(screen.getByText("d.pdf")).toBeTruthy();
});
