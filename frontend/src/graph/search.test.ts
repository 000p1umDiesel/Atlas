import { DEFAULT_FILTERS } from "./model";
import { DEFAULT_GRAPH_SEARCH, graphLink, parseGraphSearch, serializeGraphSearch } from "./search";

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
    expect(parseGraphSearch({})).toEqual({
      ...DEFAULT_FILTERS,
      view: "2d",
      focus: null,
      mode: "overview",
      center: null,
      depth: 1,
      source: null,
      target: null,
      hops: 4,
      expand: [],
    });
    expect(parseGraphSearch({})).toEqual(DEFAULT_GRAPH_SEARCH);
    expect(serializeGraphSearch({ ...DEFAULT_FILTERS, view: "2d" })).toEqual({});
  });
  it("round-trip", () => {
    const f = {
      ...DEFAULT_GRAPH_SEARCH,
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

describe("graph search: режимы", () => {
  it("неизвестный режим → обзор", () => {
    expect(parseGraphSearch({ mode: "foo" }).mode).toBe("overview");
    for (const mode of ["overview", "neighbors", "path", "community"]) {
      expect(parseGraphSearch({ mode }).mode).toBe(mode);
    }
  });
  it("глубина вне 1–3 → 1, длина пути вне 1–5 → 4", () => {
    expect(parseGraphSearch({ depth: "9" }).depth).toBe(1);
    expect(parseGraphSearch({ depth: 0 }).depth).toBe(1);
    expect(parseGraphSearch({ depth: "2.5" }).depth).toBe(1);
    expect(parseGraphSearch({ depth: "3" }).depth).toBe(3);
    expect(parseGraphSearch({ hops: "0" }).hops).toBe(4);
    expect(parseGraphSearch({ hops: 6 }).hops).toBe(4);
    expect(parseGraphSearch({ hops: "abc" }).hops).toBe(4);
    expect(parseGraphSearch({ hops: 5 }).hops).toBe(5);
  });
  it("id концов проверяются как focus", () => {
    expect(parseGraphSearch({ center: "bad id!" }).center).toBeNull();
    expect(parseGraphSearch({ source: "<b>", target: "x".repeat(200) })).toMatchObject({ source: null, target: null });
    expect(parseGraphSearch({ center: "e1", source: 12, target: "ent-7-042" })).toMatchObject({
      center: "e1",
      source: "12",
      target: "ent-7-042",
    });
  });
  it("expand: уникальные корректные id, не больше последних 20", () => {
    const ids = Array.from({ length: 25 }, (_, i) => `e${i}`);
    expect(parseGraphSearch({ expand: ids.join(",") }).expand).toEqual(ids.slice(5));
    expect(parseGraphSearch({ expand: "a,b,a,bad id,c" }).expand).toEqual(["a", "b", "c"]);
    expect(parseGraphSearch({ expand: ["a", "b"] }).expand).toEqual(["a", "b"]);
  });
  it("predicates — список строк без повторов", () => {
    expect(parseGraphSearch({ predicates: "knows,works_at,knows" }).predicates).toEqual(["knows", "works_at"]);
  });
  it("serialize опускает дефолты режимов и пустые списки", () => {
    expect(serializeGraphSearch(DEFAULT_GRAPH_SEARCH)).toEqual({});
    expect(serializeGraphSearch({ ...DEFAULT_GRAPH_SEARCH, mode: "overview", depth: 1, hops: 4, expand: [], predicates: [] })).toEqual({});
  });
  it("round-trip режимов", () => {
    const s = {
      ...DEFAULT_GRAPH_SEARCH,
      mode: "path" as const,
      center: "c1",
      depth: 3 as const,
      source: "s1",
      target: "t1",
      hops: 2,
      expand: ["x", "y"],
      predicates: ["knows"],
    };
    const raw = serializeGraphSearch(s);
    expect(raw).toMatchObject({ mode: "path", center: "c1", depth: 3, source: "s1", target: "t1", hops: 2, expand: "x,y", predicates: "knows" });
    expect(parseGraphSearch(raw)).toEqual(s);
  });
});
