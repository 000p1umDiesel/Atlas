import { countOf, formatInt, formatScore, plural } from "./format";

const DOCS: [string, string, string] = ["документ", "документа", "документов"];

test("русские формы множественного числа", () => {
  expect([1, 2, 5, 11, 12, 21, 22, 25, 111, 104].map((n) => plural(n, DOCS))).toEqual([
    "документ",
    "документа",
    "документов",
    "документов",
    "документов",
    "документ",
    "документа",
    "документов",
    "документов",
    "документа",
  ]);
});

test("числа в русской записи", () => {
  expect(formatInt(1260)).toBe((1260).toLocaleString("ru-RU"));
  expect(formatInt(1260)).not.toBe("1260");
  expect(formatScore(0.041234)).toBe("0,0412");
  expect(countOf(1260, ["фрагмент", "фрагмента", "фрагментов"])).toBe(`${formatInt(1260)} фрагментов`);
});
