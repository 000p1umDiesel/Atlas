// Сторож: недоверенный HTML попадает в страницу только через ui/Markdown.tsx (markdown-it без
// HTML + DOMPurify). Любое другое место с этим свойством — ошибка ревью.
// Имя свойства собрано из частей, чтобы `grep -rl` по src находил только Markdown.tsx.
const NEEDLE = "dangerously" + "SetInnerHTML";

// Исходники как текст (Vite `?raw`) — тот же список, что `grep -rl` по src.
const SOURCES = import.meta.glob(["/src/**/*.ts", "/src/**/*.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

test("вставка HTML через React — только в src/ui/Markdown.tsx", () => {
  expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
  const hits = Object.entries(SOURCES)
    .filter(([, text]) => text.includes(NEEDLE))
    .map(([path]) => path);
  expect(hits).toEqual(["/src/ui/Markdown.tsx"]);
});
