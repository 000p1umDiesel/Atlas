import { MultiDirectedGraph } from "graphology";
import { firstMatch, graphHotkey, type KeyLike } from "./hotkeys";

/** Клавиша по физическому коду (раскладка не важна) и печатаемому символу. */
const key = (code: string, over: Partial<KeyLike> = {}): KeyLike => ({
  code,
  key: "",
  shiftKey: false,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  target: document.body,
  ...over,
});

describe("graphHotkey", () => {
  it("сопоставляет физические клавиши действиям в любой раскладке", () => {
    expect(graphHotkey(key("KeyF", { key: "f" }))).toBe("fit");
    expect(graphHotkey(key("KeyF", { key: "а" }))).toBe("fit"); // русская раскладка
    expect(graphHotkey(key("Digit2"))).toBe("view2d");
    expect(graphHotkey(key("Numpad3"))).toBe("view3d");
    expect(graphHotkey(key("Slash", { key: "/" }))).toBe("search");
    expect(graphHotkey(key("Slash", { key: "." }))).toBe("search"); // русская раскладка
    expect(graphHotkey(key("Slash", { key: "?", shiftKey: true }))).toBe("help");
    expect(graphHotkey(key("Digit7", { key: "?", shiftKey: true }))).toBe("help"); // «?» в русской
    expect(graphHotkey(key("Escape", { key: "Escape" }))).toBe("escape");
    expect(graphHotkey(key("Equal", { key: "=" }))).toBe("zoomIn");
    expect(graphHotkey(key("NumpadAdd"))).toBe("zoomIn");
    expect(graphHotkey(key("Minus"))).toBe("zoomOut");
    expect(graphHotkey(key("KeyX"))).toBeNull();
  });

  it("с модификаторами не срабатывает (Ctrl+F, Cmd+2 остаются браузеру)", () => {
    expect(graphHotkey(key("KeyF", { ctrlKey: true }))).toBeNull();
    expect(graphHotkey(key("Digit2", { metaKey: true }))).toBeNull();
    expect(graphHotkey(key("Digit3", { altKey: true }))).toBeNull();
  });

  it("при вводе текста работает только Esc", () => {
    const input = document.createElement("input");
    const area = document.createElement("textarea");
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    for (const target of [input, area, editable]) {
      expect(graphHotkey(key("KeyF", { target }))).toBeNull();
      expect(graphHotkey(key("Slash", { target }))).toBeNull();
    }
    expect(graphHotkey(key("Escape", { target: input }))).toBe("escape");
  });

  it("на кнопках работают, на ползунке и списке — нет (там свои клавиши)", () => {
    const button = document.createElement("button");
    expect(graphHotkey(key("KeyF", { target: button }))).toBe("fit");
    const range = document.createElement("input");
    range.type = "range";
    expect(graphHotkey(key("Digit2", { target: range }))).toBeNull();
    expect(graphHotkey(key("Digit2", { target: document.createElement("select") }))).toBeNull();
  });
});

describe("firstMatch", () => {
  const g = new MultiDirectedGraph();
  g.addNode("a", { name: "Яндекс", pagerank: 0.1, mention_count: 5 });
  g.addNode("b", { name: "Яндекс Research", pagerank: 0.4, mention_count: 2 });
  g.addNode("c", { name: "Google", pagerank: 0.9, mention_count: 9 });
  g.addNode("d", { name: "яндекс.Карты", pagerank: null, mention_count: 50 });
  const all = new Set(["a", "b", "c", "d"]);

  it("самое важное видимое совпадение без учёта регистра", () => {
    expect(firstMatch(g, all, "яндекс")).toBe("b");
    expect(firstMatch(g, new Set(["a", "c", "d"]), "  ЯНДЕКС ")).toBe("a");
  });

  it("пустой запрос или нет совпадений — null", () => {
    expect(firstMatch(g, all, "  ")).toBeNull();
    expect(firstMatch(g, all, "zzz")).toBeNull();
  });
});
