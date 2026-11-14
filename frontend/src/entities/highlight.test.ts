import { splitMatches } from "./highlight";

test("без учёта регистра, длинная игла раньше короткой", () => {
  expect(splitMatches("Ada Lovelace met ADA", ["Ada", "Ada Lovelace"])).toEqual([
    { text: "Ada Lovelace", match: true },
    { text: " met ", match: false },
    { text: "ADA", match: true },
  ]);
});

test("игла короче двух символов пропускается", () => {
  expect(splitMatches("a banana", ["a"])).toEqual([{ text: "a banana", match: false }]);
  expect(splitMatches("a banana", [" a ", ""])).toEqual([{ text: "a banana", match: false }]);
});

test("спецсимволы ищутся буквально", () => {
  expect(splitMatches("C++ и C", ["C++"])).toEqual([
    { text: "C++", match: true },
    { text: " и C", match: false },
  ]);
  expect(splitMatches("a.b axb", ["a.b"])).toEqual([
    { text: "a.b", match: true },
    { text: " axb", match: false },
  ]);
});

test("кириллица без учёта регистра; без игл и пустой текст", () => {
  expect(splitMatches("АДА и ада", ["Ада"])).toEqual([
    { text: "АДА", match: true },
    { text: " и ", match: false },
    { text: "ада", match: true },
  ]);
  expect(splitMatches("текст", [])).toEqual([{ text: "текст", match: false }]);
  expect(splitMatches("", ["Ада"])).toEqual([]);
});
