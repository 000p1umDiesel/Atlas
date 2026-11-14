import { DEFAULT_FILTERS } from "./model";
import { graphLink, parseGraphSearch, serializeGraphSearch } from "./search";

describe("graph search", () => {
  it("неверные значения дают дефолты", () => {
    const p = parseGraphSearch({ types: "Person,Org", minWeight: "abc", view: "4d" });
    expect(p.types).toEqual(["Person", "Org"]);
    expect(p.minWeight).toBe(0);
    expect(p.view).toBe("2d");
  });
  it("types принимает массив", () => {
    expect(parseGraphSearch({ types: ["A", "B"] }).types).toEqual(["A", "B"]);
  });
  it("пустой ввод = дефолты, дефолты не сериализуются", () => {
    expect(parseGraphSearch({})).toEqual({ ...DEFAULT_FILTERS, view: "2d", focus: null });
    expect(serializeGraphSearch({ ...DEFAULT_FILTERS, view: "2d" })).toEqual({});
  });
  it("round-trip", () => {
    const f = {
      types: ["Person", "Method"],
      community: "c3",
      minWeight: 0.5,
      minMentions: 4,
      hideIsolated: true,
      colorBy: "community" as const,
      sizeBy: "degree" as const,
      highlight: "Нео",
      view: "3d" as const,
      focus: "1a2b3c4d5e6f7a8b",
    };
    const s = serializeGraphSearch(f);
    expect(s.types).toBe("Person,Method");
    expect(parseGraphSearch(s)).toEqual(f);
  });
});

describe("graph search: числа из парсера TanStack", () => {
  it("числовые highlight и community превращаются в строки", () => {
    const p = parseGraphSearch({ highlight: 123, community: 7 });
    expect(p.highlight).toBe("123");
    expect(p.community).toBe("7");
  });
  it("нечисловой мусор отбрасывается", () => {
    expect(parseGraphSearch({ highlight: Number.NaN, community: {} })).toMatchObject({
      highlight: "",
      community: null,
    });
  });
});

describe("graph search: выбранный узел (focus)", () => {
  it("по умолчанию фокуса нет и в URL его нет", () => {
    expect(parseGraphSearch({}).focus).toBeNull();
    expect(serializeGraphSearch({ ...DEFAULT_FILTERS, view: "2d" })).toEqual({});
    expect(serializeGraphSearch({ ...DEFAULT_FILTERS, view: "2d", focus: null })).toEqual({});
  });
  it("round-trip id сущности (hex из ядра и id фикстур)", () => {
    for (const id of ["0123456789abcdef", "ent-7-042"]) {
      const s = serializeGraphSearch({ ...DEFAULT_FILTERS, view: "3d", focus: id });
      expect(s.focus).toBe(id);
      expect(parseGraphSearch(s)).toMatchObject({ view: "3d", focus: id });
    }
  });
  it("число из парсера — строка", () => {
    expect(parseGraphSearch({ focus: 1234 }).focus).toBe("1234");
  });
  it("мусор → нет фокуса", () => {
    for (const focus of ["", "   ", {}, ["a"], "a b", "<script>", "x".repeat(200), Number.NaN, true]) {
      expect(parseGraphSearch({ focus }).focus).toBeNull();
    }
  });
});

describe("graphLink", () => {
  it("окрестность, путь и сообщество", () => {
    expect(graphLink({ mode: "neighbors", center: "e1" })).toBe("/graph?mode=neighbors&center=e1&focus=e1");
    expect(graphLink({ mode: "path", source: "e1" })).toBe("/graph?mode=path&source=e1");
    expect(graphLink({ mode: "community", community: "7" })).toBe("/graph?mode=community&community=7");
    expect(graphLink({ mode: "community", community: "community:a b" })).toBe(
      "/graph?mode=community&community=community%3Aa+b",
    );
  });
});
