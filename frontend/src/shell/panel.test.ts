import { formatPanel, parsePanel } from "./panel";

const CHUNK = "aaaaaaaaaaaaaaaa:00001";

test("разбор параметра panel", () => {
  expect(parsePanel(`chunk:${CHUNK}`)).toEqual({ kind: "chunk", id: CHUNK });
  expect(parsePanel("entity:e1")).toEqual({ kind: "entity", id: "e1" });
  for (const bad of ["x:1", "", "chunk:", "entity:", undefined, null, 5, ["entity:e1"], { kind: "entity" }]) {
    expect(parsePanel(bad)).toBeNull();
  }
});

test("formatPanel и parsePanel — взаимно обратные", () => {
  for (const ref of [
    { kind: "chunk", id: CHUNK },
    { kind: "entity", id: "e:1 2" },
  ] as const) {
    expect(parsePanel(formatPanel(ref))).toEqual(ref);
  }
  expect(formatPanel({ kind: "entity", id: "e1" })).toBe("entity:e1");
});
