import { DEFAULT_FILTERS } from "./model";
import { parseGraphSearch, serializeGraphSearch } from "./search";

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
    expect(parseGraphSearch({})).toEqual({ ...DEFAULT_FILTERS, view: "2d" });
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
