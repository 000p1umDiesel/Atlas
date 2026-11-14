import { prepareWikiPage, renderMarkdown } from "./markdown";

const ALLOWED_URI = /^(?:https?:|#|\/api\/|\/wiki\/|\/documents\/)/i;

/** Разобрать результат в DOM — проверяем, что исполняется, а не какой текст виден. */
function dom(html: string): HTMLElement {
  const box = document.createElement("div");
  box.innerHTML = html;
  return box;
}

const ATTACKS = [
  ["<script>alert(1)</script>", "script"],
  ["<img src=x onerror=alert(1)>", "onerror"],
  ["[x](javascript:alert(1))", "javascript:"],
  ["[x](data:text/html,<b>1</b>)", "data:"],
  ['<p style="color:red">x</p>', "style="],
  ["<iframe src=/x></iframe>", "<iframe"],
] as const;

// html:false: сырой HTML остаётся видимым текстом (&lt;script&gt;), поэтому «script» в
// строке есть, но ни тега, ни обработчика, ни атрибута style в DOM нет.
it.each(ATTACKS)("neutralizes %s", (input) => {
  const box = dom(renderMarkdown(input));
  expect(box.querySelector("script, img, iframe, style, svg, math, form, input")).toBeNull();
  for (const el of box.querySelectorAll("*")) {
    for (const attr of el.getAttributeNames()) {
      expect(attr.startsWith("on")).toBe(false);
      expect(attr).not.toBe("style");
    }
  }
  for (const a of box.querySelectorAll("a[href]")) expect(a.getAttribute("href")).toMatch(ALLOWED_URI);
});

it.each(ATTACKS.filter(([, forbidden]) => ["javascript:", "data:", "<iframe"].includes(forbidden)))(
  "%s: в разметке нет и следа %s",
  (input, forbidden) => expect(renderMarkdown(input)).not.toContain(forbidden),
);

it("сырой HTML виден как текст", () => {
  expect(dom(renderMarkdown("<script>alert(1)</script>")).textContent).toContain("<script>alert(1)</script>");
});

it("ссылка с запрещённой схемой — просто текст", () => {
  const box = dom(renderMarkdown("[жми](javascript:alert(1))"));
  expect(box.querySelector("a")).toBeNull();
  expect(box.textContent?.trim()).toBe("жми");
});

it("external links open safely, internal stay", () => {
  const html = renderMarkdown("[a](https://ex.org) [[adam-smith|Адам]] [d](/documents/1)");
  expect(html).toContain('target="_blank"');
  expect(html).toContain('rel="noopener noreferrer"');
  expect(html).toContain('href="/wiki/adam-smith"');
  expect(html).toContain(">Адам<");
  expect(html).toContain('href="/documents/1"');
  const internal = dom(html).querySelector('a[href="/documents/1"]')!;
  expect(internal.hasAttribute("target")).toBe(false);
});

it("wiki-ссылка без подписи и со спецсимволами в slug", () => {
  const box = dom(renderMarkdown("[[a b/c]]"));
  const a = box.querySelector("a.wikilink")!;
  expect(a.getAttribute("href")).toBe("/wiki/a%20b%2Fc");
  expect(a.textContent).toBe("a b/c");
});

it("внутри ссылки правило не действует", () => {
  const box = dom(renderMarkdown("[см. [2]](https://ex.org)", { citations: true }));
  expect(box.querySelectorAll("a")).toHaveLength(1);
  expect(box.querySelector("a.cite")).toBeNull();
});

it("citations and footnotes only with env", () => {
  expect(renderMarkdown("см. [2]")).not.toContain("#cite:");
  expect(renderMarkdown("см. [2]", { citations: true })).toContain('href="#cite:2"');
  const id = "0123456789abcdef:00007";
  expect(renderMarkdown(`факт[^${id}]`, { footnotes: new Map([[id, 1]]) })).toContain(`href="#chunk:${id}"`);
  expect(renderMarkdown(`факт[^${id}]`, { footnotes: new Map() })).not.toContain(id);
  expect(renderMarkdown(`факт[^${id}]`)).toContain(`[^${id}]`);
});

it("номер сноски — по карте", () => {
  const id = "0123456789abcdef:00007";
  const a = dom(renderMarkdown(`факт[^${id}]`, { footnotes: new Map([[id, 3]]) })).querySelector("a.cite")!;
  expect(a.textContent).toBe("3");
});

it("prepareWikiPage strips frontmatter, H1, Sources and numbers footnotes by first use", () => {
  const a = "aaaaaaaaaaaaaaaa:00001",
    b = "bbbbbbbbbbbbbbbb:00002";
  const { body, footnotes } = prepareWikiPage(
    `---\nx: 1\n---\n# Title\nText[^${b}] and[^${a}][^${b}]\n## Sources\n[^${a}]: s`,
  );
  expect(body).not.toMatch(/Title|Sources|^---/m);
  expect([...footnotes]).toEqual([
    [b, 1],
    [a, 2],
  ]);
});

it("prepareWikiPage убирает строки определений сносок и вне раздела Sources", () => {
  const a = "aaaaaaaaaaaaaaaa:00001";
  const { body } = prepareWikiPage(`Текст[^${a}]\n\n[^${a}]: источник\n`);
  expect(body).toBe(`Текст[^${a}]`);
});

it("строка-определение `[2]: адрес` не перехватывает номер источника и не исчезает", () => {
  const box = dom(renderMarkdown("см. [2]\n\n[2]: https://evil.example", { citations: true }));
  const cites = [...box.querySelectorAll("a.cite")];
  expect(cites.length).toBeGreaterThan(0);
  for (const cite of cites) expect(cite.getAttribute("href")).toBe("#cite:2");
  // Ни одна ссылка не прячет evil.example за подписью: номер [2] ведёт к источнику, а строка
  // определения остаётся видимой; голый адрес linkify делает ссылкой с самим адресом в подписи,
  // как любой другой адрес в тексте ответа.
  for (const a of box.querySelectorAll('a[href*="evil.example"]')) {
    expect(a.textContent).toBe(a.getAttribute("href"));
  }
  expect(box.textContent).toContain("2: https://evil.example");
  // и без режима цитат определение не превращает [1] в ссылку на файл
  const plain = dom(renderMarkdown("см. [1]\n\n[1]: report.pdf"));
  expect(plain.querySelector("a")).toBeNull();
  expect(plain.textContent).toContain("[1]: report.pdf");
});

it("длинная цепочка `[[` разбирается быстро (без квадратичного перебора)", () => {
  const started = performance.now();
  const html = renderMarkdown("[[".repeat(20_000));
  // прежнее выражение тратило на это ~0,7 с, линейное — миллисекунды
  expect(performance.now() - started).toBeLessThan(250);
  expect(html).toContain("[[[[");
  // обычные wiki-ссылки по-прежнему работают
  expect(dom(renderMarkdown("[[a|b]] и [[c]]")).querySelectorAll("a.wikilink")).toHaveLength(2);
});
