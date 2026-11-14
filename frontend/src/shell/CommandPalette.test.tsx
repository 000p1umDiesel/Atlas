import { act, fireEvent, screen, within } from "@testing-library/react";
import { useState } from "react";
import { getThemeChoice, setThemeChoice } from "../lib/theme";
import { deferred, fail, fakeApi, type FakeApi } from "../test/fakeApi";
import { renderWithRouter } from "../test/router";
import { CommandPalette } from "./CommandPalette";
import { TopBar } from "./TopBar";

const TYPES = [{ type: "Person", label: "Человек", description: "", count: 2 }];
const entity = (id: string, name: string) => ({
  entity_id: id,
  name,
  type: "Person",
  mention_count: 1,
  pagerank: null,
  community: null,
  degree: 1,
  aliases: [],
  has_page: false,
  slug: null,
});
const EMPTY = { entities: [], wiki: [], documents: [], conversations: [] };
const ADA = {
  entities: [entity("a1", "Ада Лавлейс")],
  wiki: [{ page_id: "p1", slug: "ada-lovelace", title: "Ада Лавлейс" }],
  documents: [{ id: "d1", name: "ada.pdf", status: "done" }],
  conversations: [{ id: "c1", title: "Про Аду" }],
};

/** Как в оболочке: верхняя панель с кнопкой и палитра с общим состоянием. */
function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <TopBar accountOpen={false} onAccountOpenChange={() => {}} onLogout={() => {}} onSearch={() => setOpen(true)} />
      <input aria-label="Чужое поле" />
      <CommandPalette open={open} onOpenChange={setOpen} />
    </>
  );
}

async function setup(api: FakeApi, path = "/") {
  vi.stubGlobal("fetch", api);
  return renderWithRouter(<Harness />, path);
}

const searchRequests = (api: FakeApi) => api.requests().filter((r) => r.startsWith("GET /api/search/"));
const ctrlK = (target: Element | Document = document) =>
  act(() => fireEvent.keyDown(target, { key: "k", code: "KeyK", ctrlKey: true }));
const input = () => screen.getByRole("combobox", { name: "Поиск" });
const type = (value: string) => act(() => fireEvent.change(input(), { target: { value } }));
const pause = (ms: number) => act(async () => new Promise((r) => setTimeout(r, ms)));
/** Подписи групп — по aria-labelledby. */
const groupNames = () =>
  screen.queryAllByRole("group").map((g) => document.getElementById(g.getAttribute("aria-labelledby") ?? "")?.textContent);
const closed = () => vi.waitFor(() => expect(screen.queryByRole("dialog", { name: "Поиск" })).toBeNull());

afterEach(() => {
  vi.unstubAllGlobals();
  setThemeChoice("system");
});

test("Ctrl+K открывает, фокус в поле; пустой ввод — только «Действия», без запросов; повторное Ctrl+K закрывает", async () => {
  const api = fakeApi({ "GET /api/entities/types": TYPES });
  await setup(api);
  expect(screen.queryByRole("dialog")).toBeNull();
  await ctrlK();
  const dialog = await screen.findByRole("dialog", { name: "Поиск" });
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(document.activeElement).toBe(input());
  expect(groupNames()).toEqual(["Действия"]);
  expect(within(dialog).getByRole("group", { name: "Действия" })).toBeTruthy();
  expect(within(dialog).getAllByRole("option")).toHaveLength(11);
  await pause(200);
  expect(searchRequests(api)).toEqual([]);
  // Повторное нажатие — из самого поля палитры — закрывает.
  await ctrlK(input());
  await closed();
});

test("Ctrl+K работает и в поле ввода, ⌘K — тоже", async () => {
  await setup(fakeApi({ "GET /api/entities/types": TYPES }));
  await ctrlK(screen.getByRole("textbox", { name: "Чужое поле" }));
  await screen.findByRole("dialog", { name: "Поиск" });
  await act(() => fireEvent.keyDown(input(), { key: "Escape" }));
  await closed();
  await act(() => fireEvent.keyDown(document, { key: "л", code: "KeyK", metaKey: true }));
  await screen.findByRole("dialog", { name: "Поиск" });
});

test("ввод «ada» → через 150 мс запрос, группы с заголовками; ↓↓ + Enter на документе → /documents/d1", async () => {
  const api = fakeApi({ "GET /api/entities/types": TYPES, "GET /api/search/quick?q=ada": ADA });
  const { router } = await setup(api);
  await ctrlK();
  await type("ada");
  await pause(80);
  expect(searchRequests(api)).toEqual([]);
  await vi.waitFor(() => expect(searchRequests(api)).toEqual(["GET /api/search/quick?q=ada"]));
  await screen.findByRole("option", { name: /ada\.pdf/ });
  expect(groupNames()).toEqual(["Сущности", "Wiki", "Документы", "Диалоги"]);
  const options = screen.getAllByRole("option");
  expect(options[0]!.textContent).toContain("Ада Лавлейс");
  expect(options[0]!.textContent).toContain("Человек");
  // Первый пункт выделен сразу: Enter без стрелок выполнил бы его.
  expect(input().getAttribute("aria-activedescendant")).toBe(options[0]!.id);
  fireEvent.keyDown(input(), { key: "ArrowDown" });
  fireEvent.keyDown(input(), { key: "ArrowDown" });
  expect(input().getAttribute("aria-activedescendant")).toBe(options[2]!.id);
  expect(options[2]!.getAttribute("aria-selected")).toBe("true");
  await act(() => fireEvent.keyDown(input(), { key: "Enter" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/documents/d1"));
  await closed();
});

test("стрелки идут по всем пунктам подряд и по кругу; клик ведёт на страницу", async () => {
  const api = fakeApi({ "GET /api/entities/types": TYPES, "GET /api/search/quick?q=ada": ADA });
  const { router } = await setup(api);
  await ctrlK();
  await type("ada");
  await screen.findByRole("option", { name: /Про Аду/ });
  const options = screen.getAllByRole("option");
  expect(options).toHaveLength(4);
  fireEvent.keyDown(input(), { key: "ArrowUp" });
  expect(input().getAttribute("aria-activedescendant")).toBe(options[3]!.id);
  fireEvent.keyDown(input(), { key: "ArrowDown" });
  expect(input().getAttribute("aria-activedescendant")).toBe(options[0]!.id);
  fireEvent.click(screen.getByRole("option", { name: /Ада Лавлейс.*Человек/ }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/entities/a1"));
  await closed();

  await ctrlK();
  await type("ada");
  fireEvent.click(await screen.findByRole("option", { name: /Про Аду/ }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/chat/c1"));
  await closed();

  await ctrlK();
  await type("ada");
  const wiki = within(await screen.findByRole("group", { name: "Wiki" })).getByRole("option");
  fireEvent.click(wiki);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/wiki/ada-lovelace"));
});

test("поздний ответ на «ad» не заменяет ответ на «ada»", async () => {
  const late = deferred<Response>();
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/search/quick?q=ad": () => late.promise,
    "GET /api/search/quick?q=ada": ADA,
  });
  await setup(api);
  await ctrlK();
  await type("ad");
  await vi.waitFor(() => expect(searchRequests(api)).toEqual(["GET /api/search/quick?q=ad"]));
  await type("ada");
  await screen.findByRole("option", { name: /ada\.pdf/ });
  await act(async () => late.resolve(Response.json({ ...EMPTY, documents: [{ id: "d9", name: "старый.pdf", status: "done" }] })));
  await pause(50);
  expect(screen.queryByRole("option", { name: /старый\.pdf/ })).toBeNull();
  expect(screen.getByRole("option", { name: /ada\.pdf/ })).toBeTruthy();
});

test("ввод фильтрует действия; «Сменить тему» — следующая тема по кругу", async () => {
  const api = fakeApi({ "GET /api/entities/types": TYPES, "GET /api/search/quick": EMPTY });
  await setup(api);
  setThemeChoice("system");
  await ctrlK();
  await type("тему");
  await vi.waitFor(() => expect(searchRequests(api)).toHaveLength(1));
  expect(groupNames()).toEqual(["Действия"]);
  expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([expect.stringContaining("Сменить тему")]);
  await act(() => fireEvent.keyDown(input(), { key: "Enter" }));
  expect(getThemeChoice()).toBe("light");
  await closed();
});

test("пусто — «Ничего не нашлось.»; ошибка — текст сервера", async () => {
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/search/quick?q=zzz": EMPTY,
    "GET /api/search/quick?q=bad": () => fail(503, "Граф недоступен"),
  });
  await setup(api);
  await ctrlK();
  await type("zzz");
  expect(await screen.findByText("Ничего не нашлось.")).toBeTruthy();
  expect(screen.queryAllByRole("option")).toHaveLength(0);
  await type("bad");
  expect(await screen.findByText("Граф недоступен")).toBeTruthy();
});

test("Esc закрывает, обработчик keydown на document его не получает, фокус возвращается", async () => {
  const onDocKey = vi.fn();
  document.addEventListener("keydown", onDocKey);
  try {
    await setup(fakeApi({ "GET /api/entities/types": TYPES }));
    const opener = screen.getByRole("button", { name: "Поиск" });
    opener.focus();
    fireEvent.click(opener);
    await screen.findByRole("dialog", { name: "Поиск" });
    expect(document.activeElement).toBe(input());
    onDocKey.mockClear();
    await act(() => fireEvent.keyDown(input(), { key: "Escape" }));
    await closed();
    expect(onDocKey).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(opener);
  } finally {
    document.removeEventListener("keydown", onDocKey);
  }
});

test("клик по фону закрывает; Tab не уходит из диалога", async () => {
  await setup(fakeApi({ "GET /api/entities/types": TYPES }));
  fireEvent.click(screen.getByRole("button", { name: "Поиск" }));
  const dialog = await screen.findByRole("dialog", { name: "Поиск" });
  fireEvent.keyDown(input(), { key: "Tab" });
  expect(dialog.contains(document.activeElement)).toBe(true);
  fireEvent.keyDown(input(), { key: "Tab", shiftKey: true });
  expect(dialog.contains(document.activeElement)).toBe(true);
  // Клик внутри панели её не закрывает.
  fireEvent.click(dialog);
  expect(screen.getByRole("dialog", { name: "Поиск" })).toBeTruthy();
  const backdrop = document.querySelector("[data-palette-backdrop]");
  expect(backdrop).not.toBeNull();
  // Затемнение — токен --scrim (в тёмной теме --ink светлый и высветлил бы фон).
  expect(backdrop!.classList.contains("bg-scrim")).toBe(true);
  fireEvent.click(backdrop!);
  await closed();
});

test("кнопка в верхней панели — «Поиск» с подсказкой сочетания", async () => {
  await setup(fakeApi({ "GET /api/entities/types": TYPES }));
  const button = screen.getByRole("button", { name: "Поиск" });
  expect(button.querySelector("kbd")?.textContent).toMatch(/^(Ctrl K|⌘K)$/);
  fireEvent.click(button);
  expect(await screen.findByRole("dialog", { name: "Поиск" })).toBeTruthy();
});

test("устаревший ответ не цель Enter: после «ada» ввод «граф» и сразу Enter → /graph", async () => {
  const slow = deferred<Response>();
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/search/quick?q=ada": ADA,
    "GET /api/search/quick": () => slow.promise,
  });
  const { router } = await setup(api);
  await ctrlK();
  await type("ada");
  await screen.findByRole("option", { name: /ada\.pdf/ });
  // Сразу после ввода: пауза 150 мс ещё идёт, на экране — ответ на «ada».
  await type("граф");
  expect(screen.getByRole("status").textContent).toBe("Ищу…");
  expect(screen.getByRole("listbox").getAttribute("aria-busy")).toBe("true");
  const active = document.getElementById(input().getAttribute("aria-activedescendant") ?? "");
  expect(active?.textContent).toContain("Перейти: Граф");
  await act(() => fireEvent.keyDown(input(), { key: "Enter" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/graph"));
  await closed();
});

test("пока ждём новый ответ, Enter на устаревшем пункте сервера ничего не делает", async () => {
  const slow = deferred<Response>();
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/search/quick?q=ada": ADA,
    "GET /api/search/quick?q=adx": () => slow.promise,
  });
  const { router } = await setup(api);
  await ctrlK();
  await type("ada");
  await screen.findByRole("option", { name: /ada\.pdf/ });
  await type("adx");
  // Запрос на «adx» ушёл, но ответа нет: виден прежний (keepPreviousData), он устарел.
  await vi.waitFor(() => expect(searchRequests(api)).toContain("GET /api/search/quick?q=adx"));
  expect(screen.getByRole("status").textContent).toBe("Ищу…");
  expect(input().getAttribute("aria-activedescendant")).toBeNull();
  fireEvent.keyDown(input(), { key: "ArrowDown" });
  await act(() => fireEvent.keyDown(input(), { key: "Enter" }));
  expect(router.state.location.pathname).toBe("/");
  expect(screen.getByRole("dialog", { name: "Поиск" })).toBeTruthy();
  await act(async () => slow.resolve(Response.json(EMPTY)));
  expect(await screen.findByText("Ничего не нашлось.")).toBeTruthy();
});

test("повторное открытие во время закрытия начинается с пустого поля", async () => {
  await setup(fakeApi({ "GET /api/entities/types": TYPES, "GET /api/search/quick": EMPTY }));
  await ctrlK();
  await type("ada");
  await act(() => fireEvent.keyDown(input(), { key: "Escape" }));
  await ctrlK();
  const focused = document.activeElement as HTMLInputElement;
  expect(focused.getAttribute("role")).toBe("combobox");
  expect(focused.value).toBe("");
  await vi.waitFor(() => expect(screen.getAllByRole("dialog", { name: "Поиск" })).toHaveLength(1));
  expect((input() as HTMLInputElement).value).toBe("");
});
