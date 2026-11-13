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
  fake2d.api.fit.mockClear();
  fake2d.api.exportPng.mockClear();
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

test("клик по узлу открывает панель сущности", async () => {
  const { graph } = generateFixtures();
  const first = graph.nodes[0]!;
  await renderAt("/graph");
  fireEvent.click(screen.getByRole("button", { name: "выбрать узел" }));
  const sheet = await screen.findByRole("dialog", { name: first.name });
  expect(within(sheet).getByText(first.type)).toBeTruthy();
  const mentions = within(sheet).getByRole("group", { name: "Упоминания" });
  expect(within(mentions).getByText(first.mention_count.toLocaleString("ru-RU"))).toBeTruthy();
  expect((within(sheet).getByRole("button", { name: "Открыть страницу" }) as HTMLButtonElement).disabled).toBe(true);
  const rels = within(sheet).getByRole("list", { name: "Связи" });
  expect(within(rels).getAllByRole("listitem").length).toBeLessThanOrEqual(8);
  expect(fake2d.last?.selected).toBe(first.entity_id);
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
