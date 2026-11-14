import { act, fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { deferred, fakeApi, type FakeApi } from "../test/fakeApi";
import { renderWithRouter } from "../test/router";
import { EntityPicker } from "./EntityPicker";

const TYPES = [{ type: "Person", label: "Человек", description: "", count: 2 }];
const item = (id: string, name: string) => ({
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
const page = (...items: ReturnType<typeof item>[]) => ({ total: items.length, items });

function Harness({ onChange, exclude }: { onChange(id: string | null): void; exclude?: string }) {
  const [value, setValue] = useState<string | null>(null);
  return (
    <EntityPicker
      label="Сущность"
      value={value}
      exclude={exclude}
      onChange={(id) => {
        onChange(id);
        setValue(id);
      }}
    />
  );
}

async function setup(api: FakeApi, exclude?: string) {
  vi.stubGlobal("fetch", api);
  const onChange = vi.fn();
  await renderWithRouter(<Harness onChange={onChange} exclude={exclude} />);
  const input = screen.getByRole("combobox", { name: "Сущность" });
  return { input, onChange };
}

const entityRequests = (api: FakeApi) => api.requests().filter((r) => r.startsWith("GET /api/entities?"));
const type = (input: HTMLElement, value: string) => act(() => fireEvent.change(input, { target: { value } }));

afterEach(() => {
  vi.unstubAllGlobals();
});

test("ввод «ad» → через 200 мс GET /api/entities?q=ad&limit=8; ↓ + Enter выбирает", async () => {
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/entities?q=ad&limit=8": page(item("a1", "Ада Лавлейс"), item("a2", "Адам Смит")),
    "GET /api/entities/a1": { ...item("a1", "Ада Лавлейс"), description: "", descriptions: [], documents_count: 1 },
  });
  const { input, onChange } = await setup(api);
  expect(input.getAttribute("aria-expanded")).toBe("false");
  await type(input, "ad");
  await act(async () => new Promise((r) => setTimeout(r, 120)));
  expect(entityRequests(api)).toEqual([]);
  await vi.waitFor(() => expect(entityRequests(api)).toEqual(["GET /api/entities?q=ad&limit=8"]));
  const listbox = await screen.findByRole("listbox");
  expect(input.getAttribute("aria-expanded")).toBe("true");
  expect(input.getAttribute("aria-controls")).toBe(listbox.id);
  const options = await screen.findAllByRole("option");
  expect(options.map((o) => o.textContent)).toEqual(["Ада ЛавлейсЧеловек", "Адам СмитЧеловек"]);
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(input.getAttribute("aria-activedescendant")).toBe(options[0]!.id);
  expect(options[0]!.getAttribute("aria-selected")).toBe("true");
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onChange).toHaveBeenCalledWith("a1");
  expect(screen.queryByRole("listbox")).toBeNull();
  await vi.waitFor(() => expect((input as HTMLInputElement).value).toBe("Ада Лавлейс"));
  // «Очистить» снимает выбор.
  fireEvent.click(screen.getByRole("button", { name: "Очистить" }));
  expect(onChange).toHaveBeenLastCalledWith(null);
});

test("клик по варианту выбирает его; exclude скрывает вариант", async () => {
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/entities?q=ad&limit=8": page(item("a1", "Ада Лавлейс"), item("a2", "Адам Смит")),
  });
  const { input, onChange } = await setup(api, "a1");
  await type(input, "ad");
  const options = await screen.findAllByRole("option");
  expect(options.map((o) => o.textContent)).toEqual(["Адам СмитЧеловек"]);
  fireEvent.mouseDown(options[0]!);
  fireEvent.click(options[0]!);
  expect(onChange).toHaveBeenCalledWith("a2");
});

test("ответ на «a», пришедший позже ответа на «ad», не показывается", async () => {
  const slow = deferred<Response>();
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/entities?q=a&limit=8": () => slow.promise,
    "GET /api/entities?q=ad&limit=8": page(item("a1", "Ада Лавлейс")),
  });
  const { input } = await setup(api);
  await type(input, "a");
  await vi.waitFor(() => expect(entityRequests(api)).toEqual(["GET /api/entities?q=a&limit=8"]));
  await type(input, "ad");
  expect(await screen.findByRole("option", { name: /Ада Лавлейс/ })).toBeTruthy();
  await act(async () => {
    slow.resolve(Response.json(page(item("z9", "Альфа Центавра"))));
    await new Promise((r) => setTimeout(r, 50));
  });
  expect(screen.queryByRole("option", { name: /Альфа/ })).toBeNull();
  expect(screen.getAllByRole("option")).toHaveLength(1);
});

test("пустой ввод запросов не шлёт; Esc закрывает список", async () => {
  const api = fakeApi({
    "GET /api/entities/types": TYPES,
    "GET /api/entities?q=ad&limit=8": page(item("a1", "Ада Лавлейс")),
  });
  const { input } = await setup(api);
  await type(input, "   ");
  await act(async () => new Promise((r) => setTimeout(r, 300)));
  expect(entityRequests(api)).toEqual([]);
  await type(input, "ad");
  await screen.findByRole("listbox");
  fireEvent.keyDown(input, { key: "Escape" });
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(input.getAttribute("aria-expanded")).toBe("false");
  await type(input, "");
  await act(async () => new Promise((r) => setTimeout(r, 300)));
  expect(entityRequests(api)).toEqual(["GET /api/entities?q=ad&limit=8"]);
});
