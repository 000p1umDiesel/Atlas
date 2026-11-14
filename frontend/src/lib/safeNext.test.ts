import { safeNext } from "./safeNext";

it.each([
  [undefined, "/"],
  ["", "/"],
  ["/chat/abc?panel=chunk%3Ax", "/chat/abc?panel=chunk%3Ax"],
  ["//evil.example", "/"],
  ["https://evil.example", "/"],
  ["/login?next=/x", "/"],
  ["chat", "/"],
])("safeNext(%s) = %s", (raw, expected) => expect(safeNext(raw)).toBe(expected));

// Браузер читает «\» как «/» и выбрасывает табуляции и переводы строк из URL: «/\evil» и
// «/\t/evil» — тоже чужой сайт.
it.each(["/\\evil.example", "/\t/evil.example", "/\n/evil.example", "/login", "/login/"])(
  "safeNext(%j) = /",
  (raw) => expect(safeNext(raw)).toBe("/"),
);
