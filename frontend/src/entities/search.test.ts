import {
  effectiveOrder,
  effectiveSort,
  parseEntitiesSearch,
  serializeEntitiesSearch,
  withOrder,
  withSort,
} from "./search";

const EMPTY = { q: "", types: [], community: null, sort: null, order: null, minMentions: 0 };

describe("entities search", () => {
  it("неверные значения → null/дефолт, список типов через запятую", () => {
    expect(parseEntitiesSearch({ sort: "bogus", order: "up", minMentions: "-3", type: "Person,,Concept" })).toEqual({
      ...EMPTY,
      types: ["Person", "Concept"],
    });
  });

  it("relevance без q → null; с q сохраняется", () => {
    expect(parseEntitiesSearch({ sort: "relevance" }).sort).toBeNull();
    expect(parseEntitiesSearch({ sort: "relevance", q: "  " }).sort).toBeNull();
    expect(parseEntitiesSearch({ sort: "relevance", q: "ada" }).sort).toBe("relevance");
  });

  it("type — массив (повтор ключа), без повторов; числа из URL превращаются в строки", () => {
    const s = parseEntitiesSearch({ type: ["Person", "Work", "Person"], community: 7, minMentions: "4" });
    expect(s.types).toEqual(["Person", "Work"]);
    expect(s.community).toBe("7");
    expect(s.minMentions).toBe(4);
  });

  it("мусор: объект, дробь, NaN", () => {
    expect(parseEntitiesSearch({ q: {}, community: " ", minMentions: "2.5" })).toEqual({ ...EMPTY, minMentions: 2 });
    expect(parseEntitiesSearch({ minMentions: "abc" }).minMentions).toBe(0);
  });

  it("serialize(parse(x)) не пишет дефолтов", () => {
    expect(serializeEntitiesSearch(parseEntitiesSearch({}))).toEqual({});
    expect(serializeEntitiesSearch(parseEntitiesSearch({ sort: "bogus", order: "up", minMentions: "0", q: " " }))).toEqual(
      {},
    );
  });

  it("round-trip", () => {
    const s = {
      q: "ada",
      types: ["Person", "Concept"],
      community: "c3",
      sort: "pagerank" as const,
      order: "asc" as const,
      minMentions: 2,
    };
    const out = serializeEntitiesSearch(s);
    expect(out).toEqual({ q: "ada", type: "Person,Concept", community: "c3", sort: "pagerank", order: "asc", minMentions: 2 });
    expect(parseEntitiesSearch(out)).toEqual(s);
  });

  it("действующая сортировка и порядок по умолчанию", () => {
    expect(effectiveSort(EMPTY)).toBe("mentions");
    expect(effectiveSort({ ...EMPTY, q: "x" })).toBe("relevance");
    expect(effectiveOrder(EMPTY)).toBe("desc");
    expect(effectiveOrder({ ...EMPTY, sort: "name" })).toBe("asc");
  });

  it("выбор сортировки и порядка не пишет значений по умолчанию", () => {
    expect(withSort(EMPTY, "mentions")).toEqual(EMPTY);
    expect(withSort({ ...EMPTY, order: "asc" }, "pagerank")).toEqual({ ...EMPTY, sort: "pagerank" });
    expect(withOrder({ ...EMPTY, sort: "name" }, "asc")).toEqual({ ...EMPTY, sort: "name" });
    expect(withOrder({ ...EMPTY, sort: "name" }, "desc")).toEqual({ ...EMPTY, sort: "name", order: "desc" });
  });
});
