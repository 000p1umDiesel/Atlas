import { parsePlainSearch, stringifyPlainSearch } from "./searchParams";

test("значения остаются строками: числа и true не превращаются", () => {
  expect(parsePlainSearch("?highlight=123&hideIsolated=true&types=Person,Concept")).toEqual({
    highlight: "123",
    hideIsolated: "true",
    types: "Person,Concept",
  });
});

test("пустая строка и повторяющийся ключ", () => {
  expect(parsePlainSearch("")).toEqual({});
  expect(parsePlainSearch("?a=1&a=2")).toEqual({ a: ["1", "2"] });
});

test("сериализация без JSON: строки, числа, булевы; запятые не экранируются", () => {
  expect(stringifyPlainSearch({ types: "Person,Concept", minWeight: 0.5, hideIsolated: true })).toBe(
    "?types=Person,Concept&minWeight=0.5&hideIsolated=true",
  );
  expect(stringifyPlainSearch({ highlight: "123" })).toBe("?highlight=123");
  expect(stringifyPlainSearch({ a: undefined, b: null })).toBe("");
  expect(stringifyPlainSearch({})).toBe("");
});

test("туда и обратно — то же самое", () => {
  const search = { highlight: "Иван Петров & Co", types: "Person" };
  expect(parsePlainSearch(stringifyPlainSearch(search))).toEqual(search);
});
