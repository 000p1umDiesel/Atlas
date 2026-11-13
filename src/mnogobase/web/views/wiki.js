// Wiki: поиск и список страниц; страница с переходами по [[ссылкам]] и сносками на
// фрагменты документов ([^chunk_id] → номер, по клику — текст фрагмента).
import { html, useEffect, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { Markdown } from "../lib/markdown.js";
import { link } from "../lib/router.js";
import { Empty, ErrorNote, fetchPages, Loading, plural, useLoad } from "../lib/ui.js";
import { SourcePanel } from "./sources.js";

const FOOTNOTE = /\[\^([0-9a-f]{16}:\d{5})\]/g;

// Разбирает файл страницы: без frontmatter, без заголовка (он над страницей) и без раздела
// Sources с определениями сносок — их заменяет список из `sources` ответа API.
export function prepareWikiPage(content) {
  let body = content.replace(/^---\n[\s\S]*?\n---\n/, "").trimStart();
  body = body.replace(/^# .*\n+/, "");
  body = body.split(/^## Sources\s*$/m)[0];
  body = body.replace(/^\[\^[^\]]+\]:.*$/gm, "").trimEnd();
  const order = [];
  for (const m of body.matchAll(FOOTNOTE)) if (!order.includes(m[1])) order.push(m[1]);
  return { body, footnotes: new Map(order.map((id, i) => [id, i + 1])), order };
}

function WikiIndex() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [count, setCount] = useState(200);
  useEffect(() => {
    const t = setTimeout(() => {
      setQuery(q.trim());
      setCount(200);
    }, 250);
    return () => clearTimeout(t);
  }, [q]);
  const { data: page, error, loading } = useLoad(
    () => fetchPages("/api/wiki" + (query ? "?q=" + encodeURIComponent(query) : ""), count),
    [query, count],
  );
  const data = page && page.items;
  const groups = {};
  for (const p of data || []) (groups[p.entity_type] = groups[p.entity_type] || []).push(p);
  return html`
    <section class="page-wide wiki-index">
      <h1>Wiki</h1>
      <p class="muted">Страницы о людях, понятиях и работах из ваших документов. Их пишет модель по фрагментам, на которые ссылается каждая фраза.</p>
      <input class="search" type="search" placeholder="Найти страницу" value=${q} aria-label="Поиск по wiki"
        onInput=${(e) => setQ(e.currentTarget.value)} />
      <${ErrorNote} error=${error} />
      ${loading && !data && html`<${Loading} />`}
      ${data && data.length === 0 &&
      html`<${Empty}>${query ? "Ничего не нашлось. Попробуйте другое слово или псевдоним." : "Wiki пока пуста: страницы появятся после обработки документов."}<//>`}
      ${data && data.length > 0 &&
      html`<p class="muted small">${page.more ? "Показаны первые " : ""}${plural(data.length, ["страница", "страницы", "страниц"])}</p>
        <div class="wiki-groups">
          ${Object.keys(groups)
            .sort()
            .map(
              (type) => html`<div class="wiki-group" key=${type}>
                <h2>${type}</h2>
                <ul>${groups[type].map((p) => html`<li key=${p.page_id}><a href=${link("wiki", p.slug)}>${p.title}</a></li>`)}</ul>
              </div>`,
            )}
        </div>
        ${page.more && html`<button onClick=${() => setCount(count + 200)} disabled=${loading}>Показать ещё</button>`}`}
    </section>`;
}

function WikiPage({ slug, pageId }) {
  const [panel, setPanel] = useState(null);
  const { data: page, error } = useLoad(
    () => (pageId ? api("/api/wiki/" + encodeURIComponent(pageId)) : api("/api/wiki/by-slug/" + encodeURIComponent(slug))),
    [slug, pageId],
  );
  useEffect(() => setPanel(null), [slug, pageId]);
  if (error)
    return html`<section class="page-wide"><p class="crumbs"><a href="#/wiki">Wiki</a></p>
      <p class="note note-error">${error.status === 404 ? "Страница не найдена: возможно, её удалили вместе с документом." : error.detail}</p></section>`;
  if (!page) return html`<section class="page-wide"><${Loading} /></section>`;
  const { body, footnotes, order } = prepareWikiPage(page.content);
  const open = (id) => setPanel({ chunkId: id, source: { n: footnotes.get(id) } });
  return html`
    <div class="wiki-page sheet">
      <article class="wiki-column">
        <p class="crumbs"><a href="#/wiki">Wiki</a></p>
        <h1>${page.title}</h1>
        <p class="row small"><a href=${link("graph", page.id)}>Показать в графе</a></p>
        <${Markdown} text=${body} env=${{ footnotes }} onRef=${(ref) => ref.kind === "chunk" && open(ref.id)} />
        ${order.length > 0 &&
        html`<h2 class="sources-title">Источники</h2>
          <ol class="source-list">
            ${order.map(
              (id) => html`<li key=${id} class="cited"><button class="source-link" onClick=${() => open(id)}>
                <span class="source-n">${footnotes.get(id)}</span>
                <span class="source-name">${(page.sources || {})[id] || "фрагмент удалён"}</span>
              </button></li>`,
            )}
          </ol>`}
      </article>
      <${SourcePanel} target=${panel} onClose=${() => setPanel(null)} />
    </div>`;
}

export function Wiki({ parts }) {
  if (parts[1] === "page" && parts[2]) return html`<${WikiPage} pageId=${parts[2]} />`;
  if (parts[1]) return html`<${WikiPage} slug=${parts[1]} />`;
  return html`<${WikiIndex} />`;
}
