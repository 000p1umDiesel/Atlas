import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { generateFixtures } from "../fixtures/generate";
import { createAppRouter } from "../router";
import { fake2d, fake3d } from "../test/fakeGraph";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
vi.mock("../graph/Graph3D", async () => ({ default: (await import("../test/fakeGraph")).FakeGraph3D }));

async function renderAt(path: string) {
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  await screen.findByTestId(/graph2d|graph3d/);
  return router;
}

/** Клик, после которого роутер переходит асинхронно: дожидаемся перехода внутри act. */
async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
    await new Promise((r) => setTimeout(r, 0));
  });
}

const searchStr = (router: ReturnType<typeof createAppRouter>) => router.state.location.searchStr;
const filters = () => screen.getByRole("complementary", { name: "Контекст раздела" });

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});

beforeEach(() => {
  vi.stubEnv("VITE_FIXTURES", "1");
  for (const fn of Object.values(fake2d.api)) fn.mockClear();
  fake2d.last = null;
  fake3d.last = null;
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

test("клик по чипу «Person» → в URL ровно ?types=Person", async () => {
  const router = await renderAt("/graph");
  await click(within(filters()).getByRole("button", { name: /^Person/ }));
  await vi.waitFor(() => expect(searchStr(router)).toBe("?types=Person"));
  expect(fake2d.last?.filters.types).toEqual(["Person"]);
});

test("числовая строка подсветки остаётся строкой", async () => {
  const router = await renderAt("/graph?highlight=123");
  expect(router.state.location.search).toMatchObject({ highlight: "123" });
  expect(fake2d.last?.filters.highlight).toBe("123");
  expect((within(filters()).getByLabelText("Подсветить по имени") as HTMLInputElement).value).toBe("123");
  await click(within(filters()).getByRole("button", { name: /^Person/ }));
  await vi.waitFor(() => expect(searchStr(router)).toBe("?types=Person&highlight=123"));
});

test("фильтр без результатов → сообщение, сброс возвращает дефолты", async () => {
  const router = await renderAt("/graph?minMentions=100000&types=Person");
  expect(await screen.findByText("Под фильтры ничего не попало")).toBeTruthy();
  const main = screen.getByRole("main");
  await click(within(main).getByRole("button", { name: "Сбросить фильтры" }));
  await vi.waitFor(() => expect(searchStr(router)).toBe(""));
  expect(screen.queryByText("Под фильтры ничего не попало")).toBeNull();
  expect(fake2d.last?.filters.minMentions).toBe(0);
});

test("переключатель 3D → view=3d, фильтры сохранены", async () => {
  const router = await renderAt("/graph?types=Person&minWeight=0.5");
  await click(screen.getByRole("radio", { name: "3D" }));
  expect(await screen.findByTestId("graph3d")).toBeTruthy();
  const params = new URLSearchParams(searchStr(router));
  expect(params.get("view")).toBe("3d");
  expect(params.get("types")).toBe("Person");
  expect(params.get("minWeight")).toBe("0.5");
  expect(fake3d.last?.filters.types).toEqual(["Person"]);
});

test("без готового 3D-вида кнопки «Вписать» и «PNG» выключены", async () => {
  await renderAt("/graph");
  const fit = screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement;
  await vi.waitFor(() => expect(fit.disabled).toBe(false));
  fireEvent.click(fit);
  expect(fake2d.api.fit).toHaveBeenCalled();

  await click(screen.getByRole("radio", { name: "3D" }));
  await screen.findByTestId("graph3d");
  fake2d.api.fit.mockClear();
  expect((screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "PNG" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Вписать" }));
  expect(fake2d.api.fit).not.toHaveBeenCalled();
});

test("автоповорот есть только в 3D", async () => {
  await renderAt("/graph");
  expect(screen.queryByRole("switch", { name: "Автоповорот" })).toBeNull();
  await click(screen.getByRole("radio", { name: "3D" }));
  await screen.findByTestId("graph3d");
  const toggle = screen.getByRole("switch", { name: "Автоповорот" });
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  expect(fake3d.last?.autoRotate).toBe(true);
});

test("после фильтрации граф вписывается в экран", async () => {
  await renderAt("/graph");
  await vi.waitFor(() => expect((screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement).disabled).toBe(false));
  fake2d.api.fit.mockClear();
  await click(within(filters()).getByRole("button", { name: /^Person/ }));
  await vi.waitFor(() => expect(fake2d.api.fit).toHaveBeenCalled(), { timeout: 1500 });
});

test("PNG скачивается через object URL", async () => {
  const create = vi.fn(() => "blob:graph");
  const revoke = vi.fn();
  Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await renderAt("/graph");
  const png = screen.getByRole("button", { name: "PNG" }) as HTMLButtonElement;
  await vi.waitFor(() => expect(png.disabled).toBe(false));
  await act(async () => fireEvent.click(png));
  await vi.waitFor(() => expect(click).toHaveBeenCalled());
  expect(fake2d.api.exportPng).toHaveBeenCalled();
  expect(create).toHaveBeenCalled();
  const anchor = click.mock.contexts[0] as HTMLAnchorElement;
  expect(anchor.download).toMatch(/\.png$/);
});

test("режимы кроме «Обзор» недоступны до этапа 2", async () => {
  await renderAt("/graph");
  const group = screen.getByRole("group", { name: "Режим графа" });
  expect(within(group).getByRole("button", { name: "Обзор" }).getAttribute("aria-pressed")).toBe("true");
  for (const name of ["Окрестность", "Путь", "Сообщество"]) {
    const tab = within(group).getByRole("button", { name });
    expect(tab.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText(/после этапа 2/).id).toBe(tab.getAttribute("aria-describedby"));
  }
});

test("клик по узлу открывает компактную карточку; «Подробнее» раскрывает все связи; Esc закрывает", async () => {
  const { graph } = generateFixtures();
  const first = graph.nodes[0]!;
  await renderAt("/graph");
  fireEvent.click(screen.getByRole("button", { name: "выбрать узел" }));
  const card = await screen.findByRole("dialog", { name: first.name });
  expect(within(card).getByText(first.type)).toBeTruthy();
  const mentions = within(card).getByRole("group", { name: "Упоминания" });
  expect(within(mentions).getByText(first.mention_count.toLocaleString("ru-RU"))).toBeTruthy();
  expect(within(card).getByRole("group", { name: "PageRank" })).toBeTruthy();
  expect(within(card).getByRole("group", { name: "Связи" })).toBeTruthy();
  const rels = within(card).getByRole("list", { name: "Связи" });
  expect(within(rels).getAllByRole("listitem").length).toBeLessThanOrEqual(5);
  expect(fake2d.last?.selected).toBe(first.entity_id);
  expect(fake2d.api.focusNode).toHaveBeenCalledWith(first.entity_id, expect.objectContaining({ x: expect.any(Number) }));

  const more = within(card).queryByRole("button", { name: /Подробнее/ });
  if (more) {
    await click(more);
    expect(await within(card).findByRole("list", { name: "Остальные связи" })).toBeTruthy();
  }

  await act(async () => {
    fireEvent.keyDown(document.body, { key: "Escape", code: "Escape" });
    await new Promise((r) => setTimeout(r, 400));
  });
  await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: first.name })).toBeNull());
  expect(fake2d.last?.selected).toBeNull();
});

test("клик по пустому месту холста снимает выделение", async () => {
  await renderAt("/graph");
  fireEvent.click(screen.getByRole("button", { name: "выбрать узел" }));
  await vi.waitFor(() => expect(fake2d.last?.selected).not.toBeNull());
  act(() => fake2d.last?.onBackgroundClick?.());
  await vi.waitFor(() => expect(fake2d.last?.selected).toBeNull());
});

test("горячие клавиши: F — вписать, 3 — 3D, ? — подсказка по клавишам", async () => {
  const router = await renderAt("/graph");
  await vi.waitFor(() => expect((screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement).disabled).toBe(false));
  fake2d.api.fit.mockClear();
  fireEvent.keyDown(document.body, { key: "f", code: "KeyF" });
  expect(fake2d.api.fit).toHaveBeenCalled();
  fireEvent.keyDown(document.body, { key: "?", code: "Slash", shiftKey: true });
  expect(await screen.findByRole("region", { name: "Горячие клавиши" })).toBeTruthy();
  await act(async () => {
    fireEvent.keyDown(document.body, { key: "3", code: "Digit3" });
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(new URLSearchParams(searchStr(router)).get("view")).toBe("3d"));
});

test("кнопки масштаба зовут zoomIn / zoomOut", async () => {
  await renderAt("/graph");
  const plus = screen.getByRole("button", { name: "Приблизить" }) as HTMLButtonElement;
  await vi.waitFor(() => expect(plus.disabled).toBe(false));
  fireEvent.click(plus);
  fireEvent.click(screen.getByRole("button", { name: "Отдалить" }));
  expect(fake2d.api.zoomIn).toHaveBeenCalled();
  expect(fake2d.api.zoomOut).toHaveBeenCalled();
});

test("легенда: клик по типу — фильтр в URL, наведение — подсветка узлов", async () => {
  const router = await renderAt("/graph");
  await vi.waitFor(() => expect((screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement).disabled).toBe(false));
  const legend = screen.getByRole("list", { name: "Цвета типов" });
  const person = within(legend).getByRole("button", { name: "Person" });
  fireEvent.pointerEnter(person);
  const ids = fake2d.api.emphasize.mock.calls.at(-1)?.[0] as Set<string>;
  expect(ids.size).toBeGreaterThan(0);
  await click(person);
  await vi.waitFor(() => expect(searchStr(router)).toBe("?types=Person"));
});

test("Enter в поле подсветки выбирает самое важное совпадение", async () => {
  const { graph } = generateFixtures();
  const target = graph.nodes[0]!;
  await renderAt("/graph");
  const field = within(filters()).getByLabelText("Подсветить по имени");
  fireEvent.change(field, { target: { value: target.name } });
  await act(async () => {
    fireEvent.keyDown(field, { key: "Enter", code: "Enter" });
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(fake2d.last?.selected).not.toBeNull());
  const chosen = graph.nodes.find((n) => n.entity_id === fake2d.last?.selected)!;
  expect(chosen.name.toLowerCase()).toContain(target.name.toLowerCase());
});

test("чипы типов показывают число видимых сущностей", async () => {
  const { types } = generateFixtures();
  await renderAt("/graph");
  const person = types.find((t) => t.type === "Person")!;
  expect(within(filters()).getByRole("button", { name: /^Person/ }).textContent).toContain(String(person.count));
});

test("без фикстур — спокойное сообщение вместо графа", async () => {
  vi.stubEnv("VITE_FIXTURES", "");
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/graph"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  expect(await screen.findByText(/API появится на этапе 2/, {}, { timeout: 3000 })).toBeTruthy();
});
