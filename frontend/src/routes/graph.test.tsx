import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { generateFixtures } from "../fixtures/generate";
import { graphLink } from "../graph/search";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";
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
  for (const fn of Object.values(fake3d.api)) fn.mockClear();
  fake2d.last = null;
  fake3d.last = null;
  fake3d.ready = false;
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

test("клик по чипу «Человек» → в URL ровно ?types=Person", async () => {
  const router = await renderAt("/graph");
  await click(within(filters()).getByRole("button", { name: /^Человек/ }));
  await vi.waitFor(() => expect(searchStr(router)).toBe("?types=Person"));
  expect(fake2d.last?.filters.types).toEqual(["Person"]);
});

test("числовая строка подсветки остаётся строкой", async () => {
  const router = await renderAt("/graph?highlight=123");
  expect(router.state.location.search).toMatchObject({ highlight: "123" });
  expect(fake2d.last?.filters.highlight).toBe("123");
  expect((within(filters()).getByLabelText("Подсветить по имени") as HTMLInputElement).value).toBe("123");
  await click(within(filters()).getByRole("button", { name: /^Человек/ }));
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
  await click(within(filters()).getByRole("button", { name: /^Человек/ }));
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

test("в режиме фикстур режимы кроме «Обзор» недоступны: «Доступно на живых данных»", async () => {
  const router = await renderAt("/graph");
  const group = screen.getByRole("group", { name: "Режим графа" });
  expect(within(group).getByRole("button", { name: "Обзор" }).getAttribute("aria-pressed")).toBe("true");
  for (const name of ["Окрестность", "Путь", "Сообщество"]) {
    const tab = within(group).getByRole("button", { name });
    expect(tab.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText("Доступно на живых данных").id).toBe(tab.getAttribute("aria-describedby"));
  }
  await click(within(group).getByRole("button", { name: "Окрестность" }));
  expect(searchStr(router)).toBe("");
});

test("в режиме фикстур раскрытие соседей выключено: «Показать связи» — «Доступно на живых данных»", async () => {
  const router = await renderAt("/graph");
  expect(fake2d.last?.onExpand).toBeUndefined();
  fireEvent.click(screen.getByRole("button", { name: "выбрать узел" }));
  const card = await screen.findByRole("dialog");
  const expand = within(card).getByRole("button", { name: /Показать связи/ });
  expect(expand.getAttribute("aria-disabled")).toBe("true");
  expect(expand.getAttribute("title")).toBe("Доступно на живых данных");
  await click(expand);
  expect(new URLSearchParams(searchStr(router)).get("expand")).toBeNull();
});

test("клик по узлу открывает компактную карточку; «Подробнее» раскрывает все связи; Esc закрывает", async () => {
  const { graph } = generateFixtures();
  const first = graph.nodes[0]!;
  await renderAt("/graph");
  fireEvent.click(screen.getByRole("button", { name: "выбрать узел" }));
  const card = await screen.findByRole("dialog", { name: first.name });
  const { types } = generateFixtures();
  const label = types.find((t) => t.type === first.type)!.label;
  expect(within(card).getByText(label)).toBeTruthy();
  expect(within(card).queryByText(first.type)).toBeNull();
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

test("вписать в обзоре: слева места не нужно, при открытой карточке узла — место справа", async () => {
  await renderAt("/graph");
  const fit = screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement;
  await vi.waitFor(() => expect(fit.disabled).toBe(false));
  fireEvent.click(fit);
  expect(fake2d.api.fit).toHaveBeenLastCalledWith(expect.objectContaining({ left: 0, right: 0 }));
  await click(screen.getByRole("button", { name: "выбрать узел" }));
  await screen.findByRole("dialog");
  fireEvent.click(fit);
  expect(fake2d.api.fit).toHaveBeenLastCalledWith(expect.objectContaining({ left: 0, right: 332 }));
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
  const person = within(legend).getByRole("button", { name: "Человек" });
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

test("Enter по уже выбранному совпадению снова наводит камеру на него", async () => {
  const { graph } = generateFixtures();
  const target = graph.nodes[0]!;
  await renderAt("/graph");
  const field = within(filters()).getByLabelText("Подсветить по имени");
  fireEvent.change(field, { target: { value: target.name } });
  const enter = async () =>
    act(async () => {
      fireEvent.keyDown(field, { key: "Enter", code: "Enter" });
      await new Promise((r) => setTimeout(r, 0));
    });
  await enter();
  await vi.waitFor(() => expect(fake2d.api.focusNode).toHaveBeenCalled());
  const calls = fake2d.api.focusNode.mock.calls.length;
  await enter();
  await vi.waitFor(() => expect(fake2d.api.focusNode.mock.calls.length).toBeGreaterThan(calls));
  expect(fake2d.api.focusNode.mock.calls.at(-1)?.[0]).toBe(fake2d.last?.selected);
});

test("чипы типов показывают число видимых сущностей", async () => {
  const { types } = generateFixtures();
  await renderAt("/graph");
  const person = types.find((t) => t.type === "Person")!;
  expect(within(filters()).getByRole("button", { name: /^Человек/ }).textContent).toContain(String(person.count));
});

test("без сессии — переход на вход с next=/graph", async () => {
  vi.stubEnv("VITE_FIXTURES", "");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "Требуется вход" }, { status: 401 })));
  try {
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/graph"] }) });
    await router.load();
    render(<App router={router} client={createQueryClient()} />);
    await vi.waitFor(() => expect(router.state.location.href).toBe("/login?next=%2Fgraph"));
    expect(screen.queryByRole("navigation", { name: "Разделы" })).toBeNull();
  } finally {
    vi.unstubAllGlobals();
  }
});

describe("выбранный узел живёт в URL (focus) и переживает смену 2D↔3D", () => {
  const params = (router: ReturnType<typeof createAppRouter>) => new URLSearchParams(searchStr(router));

  test("выбор узла пишет ?focus=<id> заменой записи истории; Esc убирает", async () => {
    const { graph } = generateFixtures();
    const first = graph.nodes[0]!;
    const router = await renderAt("/graph");
    const before = router.history.length;
    await click(screen.getByRole("button", { name: "выбрать узел" }));
    await vi.waitFor(() => expect(params(router).get("focus")).toBe(first.entity_id));
    expect(router.history.length).toBe(before);
    expect(await screen.findByRole("dialog", { name: first.name })).toBeTruthy();
    await act(async () => {
      fireEvent.keyDown(document.body, { key: "Escape", code: "Escape" });
      await new Promise((r) => setTimeout(r, 0));
    });
    await vi.waitFor(() => expect(params(router).get("focus")).toBeNull());
  });

  test("ссылка с ?focus= открывает карточку и наводит камеру", async () => {
    const { graph } = generateFixtures();
    const node = graph.nodes[5]!;
    await renderAt(`/graph?focus=${node.entity_id}`);
    expect(await screen.findByRole("dialog", { name: node.name })).toBeTruthy();
    expect(fake2d.last?.selected).toBe(node.entity_id);
    await vi.waitFor(() => expect(fake2d.api.focusNode).toHaveBeenCalledWith(node.entity_id, expect.anything()));
  });

  test("неизвестный id в focus снимается", async () => {
    const router = await renderAt("/graph?focus=nope-404");
    await vi.waitFor(() => expect(params(router).get("focus")).toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("смена вида сохраняет focus и карточку; новый рендерер наводится на узел, а не вписывает всё", async () => {
    const { graph } = generateFixtures();
    const node = graph.nodes[3]!;
    fake3d.ready = true;
    const router = await renderAt(`/graph?focus=${node.entity_id}`);
    await screen.findByRole("dialog", { name: node.name });
    await vi.waitFor(() => expect(fake2d.api.focusNode).toHaveBeenCalled());

    await click(screen.getByRole("radio", { name: "3D" }));
    await screen.findByTestId("graph3d");
    expect(params(router).get("view")).toBe("3d");
    expect(params(router).get("focus")).toBe(node.entity_id);
    expect(screen.getByRole("dialog", { name: node.name })).toBeTruthy();
    expect(fake3d.last?.selected).toBe(node.entity_id);
    await vi.waitFor(() => expect(fake3d.api.focusNode).toHaveBeenCalledWith(node.entity_id, expect.anything()));
    // Отложенное «вписать» (200 мс) при смене вида с фокусом не срабатывает.
    await act(async () => new Promise((r) => setTimeout(r, 400)));
    expect(fake3d.api.fit).not.toHaveBeenCalled();

    fake2d.api.fit.mockClear();
    fake2d.api.focusNode.mockClear();
    await click(screen.getByRole("radio", { name: "2D" }));
    await screen.findByTestId("graph2d");
    expect(params(router).get("focus")).toBe(node.entity_id);
    await vi.waitFor(() => expect(fake2d.api.focusNode).toHaveBeenCalledWith(node.entity_id, expect.anything()));
    await act(async () => new Promise((r) => setTimeout(r, 400)));
    expect(fake2d.api.fit).not.toHaveBeenCalled();
  });

  test("без фокуса смена вида по-прежнему вписывает граф", async () => {
    fake3d.ready = true;
    await renderAt("/graph");
    await click(screen.getByRole("radio", { name: "3D" }));
    await screen.findByTestId("graph3d");
    await vi.waitFor(() => expect(fake3d.api.fit).toHaveBeenCalled(), { timeout: 1500 });
  });

  test("фильтр, скрывший выбранный узел, снимает выделение и focus", async () => {
    const { graph, types } = generateFixtures();
    const node = graph.nodes[0]!;
    const other = types.find((t) => t.type !== node.type)!;
    const router = await renderAt(`/graph?focus=${node.entity_id}`);
    await screen.findByRole("dialog", { name: node.name });
    await click(within(filters()).getByRole("button", { name: new RegExp(`^${other.label} `) }));
    await vi.waitFor(() => expect(params(router).get("focus")).toBeNull());
    expect(params(router).get("types")).toBe(other.type);
    await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: node.name })).toBeNull());
    expect(fake2d.last?.selected).toBeNull();
  });
});

test("Esc на открытой подсказке «i» закрывает только её: карточка, focus и фокус кнопки остаются", async () => {
  const { graph, types } = generateFixtures();
  const node = graph.nodes[0]!;
  const info = types.find((t) => t.type === node.type)!;
  const router = await renderAt(`/graph?focus=${node.entity_id}`);
  const card = await screen.findByRole("dialog", { name: node.name });
  const button = within(card).getByRole("button", { name: `Что такое «${info.label}»` });
  act(() => button.focus()); // в jsdom Tab не переводит фокус: фокус с клавиатуры — focus()
  // Подсказка с определением (у панели режимов своя постоянная подсказка role="tooltip").
  const hint = () => screen.queryAllByRole("tooltip").find((t) => t.classList.contains("type-hint")) ?? null;
  expect(hint()?.textContent).toContain(info.description);
  await act(async () => {
    fireEvent.keyDown(button, { key: "Escape", code: "Escape" });
    await new Promise((r) => setTimeout(r, 50));
  });
  expect(hint()).toBeNull();
  expect(screen.getByRole("dialog", { name: node.name })).toBeTruthy();
  expect(new URLSearchParams(searchStr(router)).get("focus")).toBe(node.entity_id);
  expect(document.activeElement).toBe(button);
  // Второй Esc — уже обычный: снимает выделение.
  await act(async () => {
    fireEvent.keyDown(button, { key: "Escape", code: "Escape" });
    await new Promise((r) => setTimeout(r, 0));
  });
  await vi.waitFor(() => expect(new URLSearchParams(searchStr(router)).get("focus")).toBeNull());
});

test("смена фильтров при выбранном узле, который остаётся видимым, — камера снова на нём, без «вписать»", async () => {
  const { graph } = generateFixtures();
  const node = graph.nodes.find((n) => n.community !== null)!;
  await renderAt(`/graph?focus=${node.entity_id}`);
  const card = await screen.findByRole("dialog", { name: node.name });
  await vi.waitFor(() => expect(fake2d.api.focusNode).toHaveBeenCalled());
  fake2d.api.fit.mockClear();
  fake2d.api.focusNode.mockClear();
  await click(within(card).getByRole("button", { name: "Показать сообщество" }));
  await act(async () => new Promise((r) => setTimeout(r, 400)));
  expect(fake2d.api.focusNode).toHaveBeenCalledWith(node.entity_id, expect.anything());
  expect(fake2d.api.fit).not.toHaveBeenCalled();
});

test("ползунок веса связи — в единицах веса, диапазон по данным графа", async () => {
  const maxWeight = Math.max(...generateFixtures().graph.relations.map((r) => r.weight));
  expect(maxWeight).toBeGreaterThan(1);
  const router = await renderAt("/graph?minWeight=12");
  const slider = within(filters()).getByLabelText("Мин. вес связи") as HTMLInputElement;
  expect(slider.getAttribute("aria-valuetext")).toBe("12");
  expect(Number(slider.value)).toBe(Math.round(Math.sqrt(12 / maxWeight) * 100));
  await act(async () => {
    fireEvent.change(slider, { target: { value: "100" } });
  });
  await vi.waitFor(() => expect(new URLSearchParams(searchStr(router)).get("minWeight")).toBe(String(maxWeight)));
  expect(fake2d.last?.filters.minWeight).toBe(maxWeight);
});

test("карточка узла: размер сообщества с согласованным существительным", async () => {
  const { graph } = generateFixtures();
  const node = graph.nodes.find((n) => n.community !== null)!;
  const size = graph.nodes.filter((n) => n.community === node.community).length;
  const word = size % 10 === 1 && size % 100 !== 11 ? "участника" : "участников";
  await renderAt(`/graph?focus=${node.entity_id}`);
  const card = await screen.findByRole("dialog", { name: node.name });
  expect(within(card).getByText(`сообщество из ${size} ${word}`)).toBeTruthy();
});

describe("живые данные: режимы «Окрестность» и «Сообщество»", () => {
  const node = (id: string, name: string, community: string | null = "7") => ({
    entity_id: id,
    name,
    type: "Person",
    mention_count: 3,
    pagerank: 0.1,
    community,
    degree: 1,
  });
  const relation = (src: string, dst: string) => ({
    src_id: src,
    src_name: src,
    predicate: "knows",
    dst_id: dst,
    dst_name: dst,
    description: "",
    weight: 1,
    evidence: [],
  });
  const TYPES = [{ type: "Person", label: "Человек", description: "", count: 3 }];
  const OVERVIEW = {
    nodes: [node("e1", "Ада Лавлейс"), node("e2", "Бэббидж")],
    relations: [relation("e2", "e1")],
    ranked_by: "pagerank",
    truncated: false,
    total_nodes: 2,
  };
  const NEIGHBORS = { nodes: [node("e1", "Ада Лавлейс"), node("e3", "Менабреа")], relations: [relation("e1", "e3")] };
  const COMMUNITIES = {
    ready: true,
    total: 2,
    items: [
      { community: "7", size: 12, top: [node("e1", "Ада Лавлейс"), node("e2", "Бэббидж")], types: { Person: 10, Concept: 2 } },
      { community: "9", size: 4, top: [node("e5", "Тьюринг", "9")], types: { Person: 4 } },
    ],
  };
  const COMMUNITY_7 = {
    community: "7",
    size: 12,
    nodes: [node("e1", "Ада Лавлейс"), node("e2", "Бэббидж")],
    relations: [{ ...relation("e2", "e1"), src_type: "Person", dst_type: "Person" }],
    truncated: true,
  };

  type Routes = Parameters<typeof fakeApi>[0];
  const routes = (extra: Routes = {}): Routes => ({
    "GET /api/auth/me": { id: "u1", username: "owner" },
    "GET /api/entities/types": TYPES,
    "GET /api/graph/overview?limit=500": OVERVIEW,
    "GET /api/graph/communities?limit=50": COMMUNITIES,
    "GET /api/graph/communities/7?limit=300": COMMUNITY_7,
    "GET /api/graph/e1/neighbors?depth=1&limit=200": NEIGHBORS,
    "GET /api/graph/e1/neighbors?depth=2&limit=200": NEIGHBORS,
    "GET /api/entities/e1": { ...node("e1", "Ада Лавлейс"), aliases: [], description: "", descriptions: [], documents_count: 1, has_page: false, slug: null },
    ...extra,
  });

  async function renderLive(path: string, api: FakeApi) {
    vi.stubGlobal("fetch", api);
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
    await router.load();
    render(<App router={router} client={createQueryClient()} />);
    return router;
  }
  const params = (router: ReturnType<typeof createAppRouter>) => new URLSearchParams(searchStr(router));
  const modes = () => screen.getByRole("group", { name: "Режим графа" });
  const graphNodes = () => fake2d.last?.graph.nodes().sort();

  beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
  afterEach(() => vi.unstubAllGlobals());

  test("переключатель режимов рабочий: aria-pressed, без подсказки про этап 2", async () => {
    const router = await renderLive("/graph", fakeApi(routes()));
    await screen.findByTestId("graph2d");
    const group = modes();
    expect(within(group).getByRole("button", { name: "Обзор" }).getAttribute("aria-pressed")).toBe("true");
    const neighbors = within(group).getByRole("button", { name: "Окрестность" });
    expect(neighbors.getAttribute("aria-disabled")).toBeNull();
    expect(screen.queryByText(/после этапа 2/)).toBeNull();
    await click(neighbors);
    await vi.waitFor(() => expect(params(router).get("mode")).toBe("neighbors"));
    expect(neighbors.getAttribute("aria-pressed")).toBe("true");
  });

  test("?mode=neighbors без центра → просьба выбрать сущность, запроса окрестности нет", async () => {
    const api = fakeApi(routes());
    await renderLive("/graph?mode=neighbors", api);
    expect(await screen.findByText("Выберите сущность, чтобы увидеть её окрестность.")).toBeTruthy();
    expect(within(modes()).getByRole("button", { name: "Окрестность" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("combobox", { name: "Сущность" })).toBeTruthy();
    expect(api.requests().some((r) => r.includes("/neighbors"))).toBe(false);
    expect(api.requests()).not.toContain("GET /api/graph/overview?limit=500");
  });

  test("?mode=neighbors&center=e1&depth=2 → запрос depth=2&limit=200, узлы ответа на графе", async () => {
    const api = fakeApi(routes());
    await renderLive("/graph?mode=neighbors&center=e1&depth=2", api);
    await screen.findByTestId("graph2d");
    expect(api.requests()).toContain("GET /api/graph/e1/neighbors?depth=2&limit=200");
    expect(graphNodes()).toEqual(["e1", "e3"]);
    const depth = screen.getByRole("radiogroup", { name: "Глубина" });
    expect(within(depth).getByRole("radio", { name: "2" }).getAttribute("aria-checked")).toBe("true");
    // Выбранная сущность — имя в поле.
    await vi.waitFor(() =>
      expect((screen.getByRole("combobox", { name: "Сущность" }) as HTMLInputElement).value).toBe("Ада Лавлейс"),
    );
    expect(screen.queryByText("Показаны не все связи")).toBeNull();
  });

  test("смена глубины пишет depth и шлёт новый запрос", async () => {
    const api = fakeApi(routes());
    const router = await renderLive("/graph?mode=neighbors&center=e1", api);
    await screen.findByTestId("graph2d");
    await click(within(screen.getByRole("radiogroup", { name: "Глубина" })).getByRole("radio", { name: "2" }));
    await vi.waitFor(() => expect(params(router).get("depth")).toBe("2"));
    await vi.waitFor(() => expect(api.requests()).toContain("GET /api/graph/e1/neighbors?depth=2&limit=200"));
  });

  test("404 центра → «Сущность не найдена…»", async () => {
    const api = fakeApi(routes({ "GET /api/graph/gone/neighbors?depth=1&limit=200": () => fail(404, "Сущность не найдена") }));
    await renderLive("/graph?mode=neighbors&center=gone", api);
    expect(await screen.findByText("Сущность не найдена: возможно, её удалили вместе с документом.")).toBeTruthy();
  });

  test("прочая ошибка → detail сервера", async () => {
    const api = fakeApi(routes({ "GET /api/graph/e1/neighbors?depth=1&limit=200": () => fail(503, "Граф недоступен") }));
    await renderLive("/graph?mode=neighbors&center=e1", api);
    // 503 повторяется один раз (через секунду), затем — текст сервера.
    expect(await screen.findByText(/Граф недоступен\./, {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByText("Граф пока недоступен")).toBeTruthy();
  });

  test("ссылки graphLink приводят в нужный режим", async () => {
    const api = fakeApi(routes());
    const router = await renderLive("/graph", api);
    await screen.findByTestId("graph2d");
    await act(async () => {
      await router.navigate({ href: graphLink({ mode: "neighbors", center: "e1" }) });
    });
    expect(params(router).get("mode")).toBe("neighbors");
    expect(params(router).get("center")).toBe("e1");
    expect(params(router).get("focus")).toBe("e1");
    await vi.waitFor(() => expect(api.requests()).toContain("GET /api/graph/e1/neighbors?depth=1&limit=200"));
    await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e3"]));
    await act(async () => {
      await router.navigate({ href: graphLink({ mode: "community", community: "7" }) });
    });
    expect(params(router).get("mode")).toBe("community");
    expect(params(router).get("community")).toBe("7");
    await vi.waitFor(() => expect(api.requests()).toContain("GET /api/graph/communities/7?limit=300"));
    await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e2"]));
  });

  test("?mode=community без анализа → объяснение и «Запустить анализ» → POST /api/graph/analyze", async () => {
    const api = fakeApi(
      routes({
        "GET /api/graph/communities?limit=50": { ready: false, total: 0, items: [] },
        "POST /api/graph/analyze": () => Response.json({ id: 1, status: "pending" }, { status: 202 }),
      }),
    );
    await renderLive("/graph?mode=community", api);
    expect(await screen.findByText("Сообщества появятся после анализа графа.")).toBeTruthy();
    expect(screen.getByText("Выберите сообщество.")).toBeTruthy();
    await click(screen.getByRole("button", { name: "Запустить анализ" }));
    await vi.waitFor(() => expect(api.requests()).toContain("POST /api/graph/analyze"));
  });

  test("во время анализа кнопка — «Анализ идёт…» и выключена", async () => {
    const api = fakeApi(
      routes({
        "GET /api/graph/communities?limit=50": { ready: false, total: 0, items: [] },
        "GET /api/graph/analysis": { ready: false, job: { status: "running" } },
      }),
    );
    await renderLive("/graph?mode=community", api);
    const busy = (await screen.findByRole("button", { name: "Анализ идёт…" })) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
  });

  test("клик по сообществу → community=<id>, запрос подграфа, «Показаны не все связи»", async () => {
    const api = fakeApi(routes());
    const router = await renderLive("/graph?mode=community", api);
    const list = await screen.findByRole("list", { name: "Сообщества" });
    const row = within(list).getByRole("button", { name: /Ада Лавлейс, Бэббидж/ });
    expect(row.textContent).toContain("12 участников");
    expect(row.getAttribute("aria-current")).toBeNull();
    await click(row);
    await vi.waitFor(() => expect(params(router).get("community")).toBe("7"));
    expect(params(router).get("mode")).toBe("community");
    await vi.waitFor(() => expect(api.requests()).toContain("GET /api/graph/communities/7?limit=300"));
    await screen.findByTestId("graph2d");
    expect(graphNodes()).toEqual(["e1", "e2"]);
    expect(within(list).getByRole("button", { name: /Ада Лавлейс, Бэббидж/ }).getAttribute("aria-current")).toBe("true");
    expect(screen.getByText("Показаны не все связи")).toBeTruthy();
    // Выбор сообщества — в карточке режима, блок в фильтрах скрыт.
    expect(within(filters()).queryByRole("combobox", { name: "Сообщество" })).toBeNull();
  });

  test("404 сообщества → объяснение и «К списку сообществ» снимает community", async () => {
    const api = fakeApi(routes({ "GET /api/graph/communities/old?limit=300": () => fail(404, "Сообщество не найдено") }));
    const router = await renderLive("/graph?mode=community&community=old", api);
    expect(await screen.findByText("Сообщество не найдено: анализ графа мог устареть.")).toBeTruthy();
    await click(screen.getByRole("button", { name: "К списку сообществ" }));
    await vi.waitFor(() => expect(params(router).get("community")).toBeNull());
    expect(params(router).get("mode")).toBe("community");
    expect(await screen.findByText("Выберите сообщество.")).toBeTruthy();
  });

  test("смена режима очищает focus и expand", async () => {
    const router = await renderLive("/graph?mode=neighbors&center=e1&focus=e1&expand=e3", fakeApi(routes()));
    await screen.findByTestId("graph2d");
    expect(params(router).get("focus")).toBe("e1");
    await click(within(modes()).getByRole("button", { name: "Обзор" }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBeNull());
    expect(params(router).get("focus")).toBeNull();
    expect(params(router).get("expand")).toBeNull();
    // Центр окрестности запоминается до возврата в режим.
    expect(params(router).get("center")).toBe("e1");
  });

  test("выход из режима «Сообщество» снимает выбранное сообщество; сброс фильтров в режиме его не трогает", async () => {
    const router = await renderLive("/graph?mode=community&community=7&types=Person", fakeApi(routes()));
    await screen.findByTestId("graph2d");
    await click(within(filters()).getByRole("button", { name: "Сбросить фильтры" }));
    await vi.waitFor(() => expect(params(router).get("types")).toBeNull());
    expect(params(router).get("community")).toBe("7");
    await click(within(modes()).getByRole("button", { name: "Обзор" }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBeNull());
    expect(params(router).get("community")).toBeNull();
  });

  test("карточка узла: «Окрестность» → mode=neighbors&center=<id>&focus=<id>", async () => {
    const router = await renderLive("/graph", fakeApi(routes()));
    await screen.findByTestId("graph2d");
    await click(screen.getByRole("button", { name: "выбрать узел" }));
    const card = await screen.findByRole("dialog", { name: "Ада Лавлейс" });
    expect(within(card).getByRole("link", { name: /Страница/ }).getAttribute("href")).toBe("/entities/e1");
    await click(within(card).getByRole("button", { name: /Окрестность/ }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBe("neighbors"));
    expect(params(router).get("center")).toBe("e1");
    expect(params(router).get("focus")).toBe("e1");
  });

  test("карточка узла: «Путь отсюда» → mode=path&source=<id>; «Показать сообщество» → mode=community", async () => {
    const router = await renderLive("/graph?focus=e1", fakeApi(routes()));
    const card = await screen.findByRole("dialog", { name: "Ада Лавлейс" });
    await click(within(card).getByRole("button", { name: /Путь отсюда/ }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBe("path"));
    expect(params(router).get("source")).toBe("e1");

    await act(async () => {
      await router.navigate({ href: "/graph?focus=e1" });
    });
    const again = await screen.findByRole("dialog", { name: "Ада Лавлейс" });
    await click(within(again).getByRole("button", { name: /Показать сообщество/ }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBe("community"));
    expect(params(router).get("community")).toBe("7");
  });

  describe("карточка узла вне обзора: метрики всего графа, а не подграфа", () => {
    const hub = { ...node("e1", "Ада Лавлейс"), degree: 17 };
    const card = () => screen.findByRole("dialog", { name: "Ада Лавлейс" });
    const metricText = (dialog: HTMLElement, label: string) => within(dialog).getByRole("group", { name: label }).textContent;

    test("окрестность: «Связи» — степень с сервера, места нет, размер сообщества — настоящий", async () => {
      const api = fakeApi(
        routes({ "GET /api/graph/e1/neighbors?depth=1&limit=200": { ...NEIGHBORS, nodes: [hub, node("e3", "Менабреа")] } }),
      );
      await renderLive("/graph?mode=neighbors&center=e1&focus=e1", api);
      const dialog = await card();
      expect(metricText(dialog, "Связи")).toContain("17");
      expect(metricText(dialog, "PageRank")).not.toMatch(/место/);
      // На графе из сообщества «7» видны 2 узла, в анализе в нём 12 участников.
      expect(await within(dialog).findByText("сообщество из 12 участников")).toBeTruthy();
      expect(within(dialog).queryByText("сообщество из 2 участников")).toBeNull();
    });

    test("окрестность: сообщества нет среди крупнейших — число участников не показывается", async () => {
      const api = fakeApi(
        routes({
          "GET /api/graph/e1/neighbors?depth=1&limit=200": { ...NEIGHBORS, nodes: [{ ...hub, community: "99" }, node("e3", "Менабреа", "99")] },
        }),
      );
      await renderLive("/graph?mode=neighbors&center=e1&focus=e1", api);
      const dialog = await card();
      await vi.waitFor(() => expect(api.requests()).toContain("GET /api/graph/communities?limit=50"));
      expect(within(dialog).getByText("сообщество")).toBeTruthy();
      expect(within(dialog).queryByText(/сообщество из/)).toBeNull();
    });

    test("сообщество: размер — из ответа сервера (на графе только верхние участники), места нет", async () => {
      const api = fakeApi(routes({ "GET /api/graph/communities/7?limit=300": { ...COMMUNITY_7, nodes: [hub, node("e2", "Бэббидж")] } }));
      await renderLive("/graph?mode=community&community=7&focus=e1", api);
      const dialog = await card();
      expect(within(dialog).getByText("сообщество из 12 участников")).toBeTruthy();
      expect(metricText(dialog, "PageRank")).not.toMatch(/место/);
      expect(metricText(dialog, "Связи")).toContain("17");
    });

    test("обзор: место по PageRank показывается", async () => {
      await renderLive("/graph?focus=e1", fakeApi(routes()));
      const dialog = await card();
      expect(metricText(dialog, "PageRank")).toMatch(/1-е место/);
    });
  });

  describe("раскрытие соседей", () => {
    const EXPAND_E1 = "GET /api/graph/e1/neighbors?depth=1&limit=40";
    const E1_NEIGHBORS = {
      nodes: [node("e1", "Ада Лавлейс"), node("e3", "Менабреа"), node("e4", "Сомервилл")],
      relations: [relation("e1", "e3"), relation("e4", "e1")],
    };
    const noticeText = () => screen.getAllByRole("status").map((el) => el.textContent ?? "").join(" ");

    test("onExpand(e1) → expand=e1, запрос depth=1&limit=40, новые узлы на графе и «Добавлено 2 сущности»", async () => {
      const api = fakeApi(routes({ [EXPAND_E1]: E1_NEIGHBORS }));
      const router = await renderLive("/graph", api);
      await screen.findByTestId("graph2d");
      expect(typeof fake2d.last?.onExpand).toBe("function");
      await vi.waitFor(() => expect((screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement).disabled).toBe(false));
      await act(async () => {
        fake2d.last?.onExpand?.("e1");
        await new Promise((r) => setTimeout(r, 0));
      });
      await vi.waitFor(() => expect(params(router).get("expand")).toBe("e1"));
      // Раскрытый узел выбран: камера остаётся на нём.
      expect(params(router).get("focus")).toBe("e1");
      await vi.waitFor(() => expect(api.requests()).toContain(EXPAND_E1));
      await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e2", "e3", "e4"]));
      await vi.waitFor(() => expect(noticeText()).toContain("Добавлено 2 сущности"));
      fake2d.api.fit.mockClear();
      await act(async () => {
        await new Promise((r) => setTimeout(r, 400));
      });
      expect(fake2d.api.focusNode).toHaveBeenLastCalledWith("e1", expect.anything());
      expect(fake2d.api.fit).not.toHaveBeenCalled();
    });

    test("повторное раскрытие того же узла — «Новых связей нет»", async () => {
      const api = fakeApi(routes({ [EXPAND_E1]: E1_NEIGHBORS }));
      await renderLive("/graph?expand=e1&focus=e1", api);
      await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e2", "e3", "e4"]));
      await act(async () => {
        fake2d.last?.onExpand?.("e1");
        await new Promise((r) => setTimeout(r, 0));
      });
      await vi.waitFor(() => expect(noticeText()).toContain("Новых связей нет"));
    });

    test("раскрытие не удалось — повторное раскрытие шлёт новый запрос и добавляет узлы", async () => {
      let calls = 0;
      const api = fakeApi(
        routes({
          // 500: Query повторяет запрос один раз — оба первых ответа ошибки, третий — соседи.
          [EXPAND_E1]: () => (++calls <= 2 ? fail(500, "Сбой") : Response.json(E1_NEIGHBORS)),
        }),
      );
      await renderLive("/graph", api);
      await screen.findByTestId("graph2d");
      await act(async () => {
        fake2d.last?.onExpand?.("e1");
        await new Promise((r) => setTimeout(r, 0));
      });
      await vi.waitFor(() => expect(noticeText()).toContain("Не удалось загрузить связи"), { timeout: 3000 });
      expect(calls).toBe(2);
      expect(graphNodes()).toEqual(["e1", "e2"]);
      await act(async () => {
        fake2d.last?.onExpand?.("e1");
        await new Promise((r) => setTimeout(r, 0));
      });
      await vi.waitFor(() => expect(calls).toBe(3));
      await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e2", "e3", "e4"]));
      await vi.waitFor(() => expect(noticeText()).toContain("Добавлено 2 сущности"));
    });

    test("21-е раскрытие вытесняет первое", async () => {
      const old = Array.from({ length: 20 }, (_, i) => `x${i + 1}`);
      const api = fakeApi(routes({ [EXPAND_E1]: E1_NEIGHBORS }));
      const router = await renderLive(`/graph?expand=${old.join(",")}`, api);
      await screen.findByTestId("graph2d");
      await act(async () => {
        fake2d.last?.onExpand?.("e1");
        await new Promise((r) => setTimeout(r, 0));
      });
      await vi.waitFor(() => expect(params(router).get("expand")).toBe([...old.slice(1), "e1"].join(",")));
      await vi.waitFor(() => expect(graphNodes()).toContain("e3"));
    });

    test("кнопка «Показать связи» в карточке делает то же", async () => {
      const api = fakeApi(routes({ [EXPAND_E1]: E1_NEIGHBORS }));
      const router = await renderLive("/graph?focus=e1", api);
      const card = await screen.findByRole("dialog", { name: "Ада Лавлейс" });
      await click(within(card).getByRole("button", { name: /Показать связи/ }));
      await vi.waitFor(() => expect(params(router).get("expand")).toBe("e1"));
      await vi.waitFor(() => expect(api.requests()).toContain(EXPAND_E1));
      await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e2", "e3", "e4"]));
      await vi.waitFor(() => expect(noticeText()).toContain("Добавлено 2 сущности"));
    });

    test("раскрытие в окрестности сливается с графом режима", async () => {
      const api = fakeApi(
        routes({
          "GET /api/graph/e3/neighbors?depth=1&limit=40": {
            nodes: [node("e3", "Менабреа"), node("e6", "Фарадей")],
            relations: [relation("e3", "e6")],
          },
        }),
      );
      await renderLive("/graph?mode=neighbors&center=e1&expand=e3", api);
      await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e3", "e6"]));
    });
  });

  test("чип типа без узлов в текущем графе не показан (выбранный — показан); легенда — так же", async () => {
    const types = [...TYPES, { type: "Concept", label: "Понятие", description: "", count: 5 }];
    const router = await renderLive("/graph", fakeApi(routes({ "GET /api/entities/types": types })));
    await screen.findByTestId("graph2d");
    await within(filters()).findByRole("button", { name: /^Человек/ });
    expect(within(filters()).queryByRole("button", { name: /^Понятие/ })).toBeNull();
    const legend = screen.getByRole("list", { name: "Цвета типов" });
    expect(within(legend).getByRole("button", { name: "Человек" })).toBeTruthy();
    expect(within(legend).queryByRole("button", { name: "Понятие" })).toBeNull();
    await act(async () => {
      await router.navigate({ href: "/graph?types=Concept" });
    });
    expect(await within(filters()).findByRole("button", { name: /^Понятие/ })).toBeTruthy();
  });

  test("фильтр связей: чипы предикатов графа, клик — predicates в URL и в фильтрах рендерера", async () => {
    const overview = {
      ...OVERVIEW,
      nodes: [...OVERVIEW.nodes, node("e3", "Менабреа")],
      relations: [relation("e2", "e1"), { ...relation("e1", "e3"), predicate: "works_at" }],
    };
    const router = await renderLive("/graph", fakeApi(routes({ "GET /api/graph/overview?limit=500": overview })));
    await screen.findByTestId("graph2d");
    const section = await within(filters()).findByRole("group", { name: "Связи" });
    const chip = within(section).getByRole("button", { name: /^works at/ });
    expect(chip.textContent).toContain("1");
    expect(within(section).getByRole("button", { name: /^knows/ })).toBeTruthy();
    await click(chip);
    await vi.waitFor(() => expect(params(router).get("predicates")).toBe("works_at"));
    expect(fake2d.last?.filters.predicates).toEqual(["works_at"]);
    expect(within(section).getByRole("button", { name: /^works at/ }).getAttribute("aria-pressed")).toBe("true");
  });

  test("предикатов больше 12 — первые 12 и «Ещё N»", async () => {
    const many = Array.from({ length: 15 }, (_, i) => ({ ...relation("e2", "e1"), predicate: `p_${String(i).padStart(2, "0")}` }));
    await renderLive("/graph", fakeApi(routes({ "GET /api/graph/overview?limit=500": { ...OVERVIEW, relations: many } })));
    await screen.findByTestId("graph2d");
    const section = await within(filters()).findByRole("group", { name: "Связи" });
    expect(within(section).getAllByRole("button", { pressed: false })).toHaveLength(12);
    await click(within(section).getByRole("button", { name: "Ещё 3" }));
    expect(within(section).getAllByRole("button", { pressed: false })).toHaveLength(15);
  });

  test("окрестность: связей в ответе столько же, сколько limit, — «Показаны не все связи»", async () => {
    const hubNodes = [node("e1", "Ада Лавлейс"), ...Array.from({ length: 50 }, (_, i) => node(`n${i}`, `Сосед ${i}`))];
    const hubRelations = Array.from({ length: 200 }, (_, i) => ({ ...relation("e1", `n${i % 50}`), predicate: `p${i}` }));
    const api = fakeApi(routes({ "GET /api/graph/e1/neighbors?depth=1&limit=200": { nodes: hubNodes, relations: hubRelations } }));
    await renderLive("/graph?mode=neighbors&center=e1", api);
    await screen.findByTestId("graph2d");
    expect(await screen.findByText("Показаны не все связи")).toBeTruthy();
  });

  test("смена режима снимает фильтр сообщества обзора; вход в «Сообщество» — сохраняет его как выбор", async () => {
    const router = await renderLive("/graph?community=7", fakeApi(routes()));
    await screen.findByTestId("graph2d");
    await click(within(modes()).getByRole("button", { name: "Окрестность" }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBe("neighbors"));
    expect(params(router).get("community")).toBeNull();

    await act(async () => {
      await router.navigate({ href: "/graph?community=7" });
    });
    await click(within(modes()).getByRole("button", { name: "Сообщество" }));
    await vi.waitFor(() => expect(params(router).get("mode")).toBe("community"));
    expect(params(router).get("community")).toBe("7");
  });

  test("выбранного узла нет в новой основной части — граф вписывается, камера не наводится на пропавший узел", async () => {
    const api = fakeApi(
      routes({
        "GET /api/graph/e2/neighbors?depth=1&limit=200": { nodes: [node("e2", "Бэббидж"), node("e5", "Тьюринг")], relations: [relation("e2", "e5")] },
      }),
    );
    // Окрестность e2 уже в кэше: смена центра без промежуточного «Собираем граф…».
    const router = await renderLive("/graph?mode=neighbors&center=e2", api);
    await vi.waitFor(() => expect(graphNodes()).toEqual(["e2", "e5"]));
    await act(async () => {
      await router.navigate({ href: "/graph?mode=neighbors&center=e1&focus=e3" });
    });
    await screen.findByRole("dialog", { name: "Менабреа" });
    await vi.waitFor(() => expect(fake2d.api.focusNode).toHaveBeenCalledWith("e3", expect.anything()));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 300));
    });
    fake2d.api.fit.mockClear();
    fake2d.api.focusNode.mockClear();
    // Выбранный e3 остаётся в URL, а в окрестности e2 его нет.
    await act(async () => {
      await router.navigate({ href: "/graph?mode=neighbors&center=e2&focus=e3" });
    });
    await vi.waitFor(() => expect(graphNodes()).toEqual(["e2", "e5"]));
    await vi.waitFor(() => expect(params(router).get("focus")).toBeNull());
    await vi.waitFor(() => expect(fake2d.api.fit).toHaveBeenCalled(), { timeout: 1500 });
    expect(fake2d.api.focusNode).not.toHaveBeenCalledWith("e3", expect.anything());
  });

  test("новый центр окрестности без выбранного узла — граф снова вписывается", async () => {
    const api = fakeApi(
      routes({
        "GET /api/graph/e2/neighbors?depth=1&limit=200": { nodes: [node("e2", "Бэббидж"), node("e1", "Ада Лавлейс")], relations: [relation("e2", "e1")] },
      }),
    );
    const router = await renderLive("/graph?mode=neighbors&center=e1", api);
    await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e3"]));
    await vi.waitFor(() => expect(fake2d.api.fit).toHaveBeenCalled(), { timeout: 1500 });
    fake2d.api.fit.mockClear();
    await act(async () => {
      await router.navigate({ href: "/graph?mode=neighbors&center=e2" });
    });
    await vi.waitFor(() => expect(graphNodes()).toEqual(["e1", "e2"]));
    await vi.waitFor(() => expect(fake2d.api.fit).toHaveBeenCalled(), { timeout: 1500 });
  });

  test("выбор сущности в карточке режима задаёт центр", async () => {
    const api = fakeApi(
      routes({
        "GET /api/entities?q=%D0%B0%D0%B4&limit=8": {
          total: 1,
          items: [{ ...node("e1", "Ада Лавлейс"), aliases: [], has_page: false, slug: null }],
        },
      }),
    );
    const router = await renderLive("/graph?mode=neighbors", api);
    const picker = await screen.findByRole("combobox", { name: "Сущность" });
    act(() => fireEvent.change(picker, { target: { value: "ад" } }));
    const option = await screen.findByRole("option", { name: /Ада Лавлейс/ });
    await click(option);
    await vi.waitFor(() => expect(params(router).get("center")).toBe("e1"));
    expect(params(router).get("focus")).toBe("e1");
    await screen.findByTestId("graph2d");
    expect(graphNodes()).toEqual(["e1", "e3"]);
  });

  describe("режим «Путь»", () => {
    const typed = (src: string, dst: string) => ({ ...relation(src, dst), src_type: "Person", dst_type: "Person" });
    const TWO_PATHS = {
      paths: [
        { nodes: [node("a", "Ада"), node("b", "Бэббидж")], relations: [typed("b", "a")] },
        { nodes: [node("a", "Ада"), node("c", "Менабреа"), node("b", "Бэббидж")], relations: [typed("a", "c"), typed("c", "b")] },
      ],
      timed_out: false,
    };
    const PATH_URL = (hops: number) => `GET /api/graph/path?source=a&target=b&max_hops=${hops}&limit=5`;
    const pathRequested = (api: FakeApi) => api.requests().some((r) => r.includes("/api/graph/path"));

    test("?mode=path&source=a → просьба выбрать две сущности, запроса пути нет", async () => {
      const api = fakeApi(routes());
      await renderLive("/graph?mode=path&source=a", api);
      expect(await screen.findByText("Выберите две сущности, чтобы найти путь между ними.")).toBeTruthy();
      expect(screen.getByRole("combobox", { name: "Откуда" })).toBeTruthy();
      expect(screen.getByRole("combobox", { name: "Куда" })).toBeTruthy();
      expect(pathRequested(api)).toBe(false);
    });

    test("source === target → «Выберите две разные сущности.», запроса нет", async () => {
      const api = fakeApi(routes());
      await renderLive("/graph?mode=path&source=a&target=a", api);
      expect(await screen.findByText("Выберите две разные сущности.")).toBeTruthy();
      expect(pathRequested(api)).toBe(false);
    });

    test("source=a&target=b&hops=3 → запрос пути, два пункта, наведение и клик подсвечивают узлы пути", async () => {
      const api = fakeApi(routes({ [PATH_URL(3)]: TWO_PATHS }));
      await renderLive("/graph?mode=path&source=a&target=b&hops=3", api);
      await screen.findByTestId("graph2d");
      expect(api.requests()).toContain(PATH_URL(3));
      expect(graphNodes()).toEqual(["a", "b", "c"]);
      const hops = screen.getByRole("radiogroup", { name: "Длина пути" });
      expect(within(hops).getByRole("radio", { name: "3" }).getAttribute("aria-checked")).toBe("true");

      const first = await screen.findByRole("button", { name: /Путь 1/ });
      const second = screen.getByRole("button", { name: /Путь 2/ });
      expect(first.textContent).toContain("Ада ← knows ← Бэббидж");
      expect(second.textContent).toContain("Путь 2 · 2 шага");
      // Ручка рендерера готова.
      await vi.waitFor(() => expect((screen.getByRole("button", { name: "Вписать" }) as HTMLButtonElement).disabled).toBe(false));

      fireEvent.mouseEnter(second);
      expect(fake2d.api.emphasize).toHaveBeenLastCalledWith(new Set(["a", "c", "b"]));
      fireEvent.mouseLeave(second);
      expect(fake2d.api.emphasize).toHaveBeenLastCalledWith(null);

      // Клик закрепляет подсветку: уход курсора её не снимает, повторный клик — снимает.
      await click(second);
      expect(second.getAttribute("aria-pressed")).toBe("true");
      expect(fake2d.api.emphasize).toHaveBeenLastCalledWith(new Set(["a", "c", "b"]));
      fireEvent.mouseEnter(first);
      expect(fake2d.api.emphasize).toHaveBeenLastCalledWith(new Set(["a", "b"]));
      fireEvent.mouseLeave(first);
      expect(fake2d.api.emphasize).toHaveBeenLastCalledWith(new Set(["a", "c", "b"]));
      await click(second);
      expect(second.getAttribute("aria-pressed")).toBe("false");
      expect(fake2d.api.emphasize).toHaveBeenLastCalledWith(null);
    });

    test("paths: [] и timed_out → текст про долгий поиск", async () => {
      const api = fakeApi(routes({ [PATH_URL(4)]: { paths: [], timed_out: true } }));
      await renderLive("/graph?mode=path&source=a&target=b", api);
      expect(await screen.findByText("Поиск пути занял слишком долго. Уменьшите длину пути.")).toBeTruthy();
      expect(screen.queryByText("Под фильтры ничего не попало")).toBeNull();
    });

    test("paths: [] при hops=3 → «Путей длиной до 3 шагов не нашлось.», кнопка ставит hops=5", async () => {
      const api = fakeApi(routes({ [PATH_URL(3)]: { paths: [], timed_out: false } }));
      const router = await renderLive("/graph?mode=path&source=a&target=b&hops=3", api);
      expect(await screen.findByText("Путей длиной до 3 шагов не нашлось.")).toBeTruthy();
      expect(screen.queryByText("Под фильтры ничего не попало")).toBeNull();
      await click(screen.getByRole("button", { name: "Искать до 5 шагов" }));
      await vi.waitFor(() => expect(params(router).get("hops")).toBe("5"));
      await vi.waitFor(() => expect(api.requests()).toContain(PATH_URL(5)));
    });

    test("«Поменять местами» меняет source и target", async () => {
      const api = fakeApi(routes({ [PATH_URL(4)]: TWO_PATHS }));
      const router = await renderLive("/graph?mode=path&source=a&target=b", api);
      await screen.findByTestId("graph2d");
      await click(screen.getByRole("button", { name: "Поменять местами" }));
      await vi.waitFor(() => expect(params(router).get("source")).toBe("b"));
      expect(params(router).get("target")).toBe("a");
    });

    test("404 конца пути → «Сущность не найдена…»", async () => {
      const api = fakeApi(routes({ [PATH_URL(4)]: () => fail(404, "Сущность не найдена") }));
      await renderLive("/graph?mode=path&source=a&target=b", api);
      expect(await screen.findByText("Сущность не найдена: возможно, её удалили вместе с документом.")).toBeTruthy();
    });

    test("вписать в режиме пути оставляет место под карточкой режима слева", async () => {
      const api = fakeApi(routes({ [PATH_URL(4)]: TWO_PATHS }));
      await renderLive("/graph?mode=path&source=a&target=b", api);
      await screen.findByTestId("graph2d");
      // Новый рендерер вписывается сам — уже с отступом слева.
      await vi.waitFor(() => expect(fake2d.api.fit).toHaveBeenCalled(), { timeout: 1500 });
      expect(fake2d.api.fit).toHaveBeenLastCalledWith(expect.objectContaining({ left: 292, right: 0 }));
      fake2d.api.fit.mockClear();
      fireEvent.click(screen.getByRole("button", { name: "Вписать" }));
      expect(fake2d.api.fit).toHaveBeenLastCalledWith(expect.objectContaining({ left: 292, right: 0 }));
      fake2d.api.fit.mockClear();
      fireEvent.keyDown(document.body, { key: "f", code: "KeyF" });
      expect(fake2d.api.fit).toHaveBeenLastCalledWith(expect.objectContaining({ left: 292 }));
    });

    test("карточка узла в режиме пути: настоящий размер сообщества", async () => {
      const api = fakeApi(routes({ [PATH_URL(4)]: TWO_PATHS }));
      await renderLive("/graph?mode=path&source=a&target=b&focus=a", api);
      const dialog = await screen.findByRole("dialog", { name: "Ада" });
      expect(await within(dialog).findByText("сообщество из 12 участников")).toBeTruthy();
    });
  });
});
