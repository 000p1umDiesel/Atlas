// Источники ответа и боковая панель с полным текстом фрагмента (общая для чата и wiki).
import { html, useEffect, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { link } from "../lib/router.js";
import { Loading } from "../lib/ui.js";

const KIND = { chunk: "документ", wiki: "wiki", entity: "сущность", relation: "связь" };

// Что открыть по источнику ответа: фрагмент документа (по chunk_id), страницу wiki или
// сущность графа; связь показывается своим текстом.
export function sourceTarget(source) {
  if (source.kind === "chunk") return { chunkId: source.ref, source };
  if (source.kind === "wiki") return { pageId: String(source.ref).split("#")[0], source };
  if (source.kind === "entity") return { entityId: source.ref, source };
  return { source };
}

function sourceName(source) {
  if (source.kind === "chunk") return source.path || "документ";
  if (source.kind === "wiki") return source.path ? source.path.replace(/\.md$/, "") : "страница wiki";
  return source.snippet ? source.snippet.split("\n")[0].slice(0, 80) : KIND[source.kind];
}

export function SourceList({ sources, onOpen }) {
  if (!sources || !sources.length) return null;
  const cited = sources.filter((s) => s.cited);
  const rest = sources.filter((s) => !s.cited);
  const item = (s) => html`
    <li class=${s.cited ? "cited" : ""}>
      <button class="source-link" onClick=${() => onOpen(sourceTarget(s))} title=${s.snippet || ""}>
        <span class="source-n">${s.n}</span>
        <span class="source-name">${sourceName(s)}</span>
        <span class="muted small">${KIND[s.kind] || s.kind}${s.page != null ? `, стр. ${s.page}` : ""}${s.available === false ? ", удалён" : ""}</span>
      </button>
    </li>`;
  return html`
    <div class="sources">
      ${cited.length > 0 && html`<ol class="source-list">${cited.map(item)}</ol>`}
      ${rest.length > 0 &&
      html`<details class="source-rest">
        <summary>${cited.length ? `Ещё ${rest.length} в контексте, без ссылок в ответе` : `Источники: ${rest.length}`}</summary>
        <ol class="source-list">${rest.map(item)}</ol>
      </details>`}
    </div>`;
}

// Панель справа: полный текст фрагмента или краткая справка с переходом.
export function SourcePanel({ target, onClose }) {
  const [state, setState] = useState({ loading: false, chunk: null, error: null });
  useEffect(() => {
    if (!target || !target.chunkId) return;
    setState({ loading: true, chunk: null, error: null });
    api("/api/chunks/" + encodeURIComponent(target.chunkId))
      .then((chunk) => setState({ loading: false, chunk, error: null }))
      .catch((error) => setState({ loading: false, chunk: null, error }));
  }, [target && target.chunkId]);
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);
  if (!target) return null;
  const source = target.source || {};
  let body;
  if (target.chunkId) {
    if (state.loading) body = html`<${Loading} />`;
    else if (state.error)
      body = html`<p class="note note-error">${state.error.status === 404 ? "Источник удалён: документа больше нет в базе." : state.error.detail}</p>`;
    else if (state.chunk) {
      const c = state.chunk;
      const [doc, idx] = c.chunk_id.split(":");
      body = html`
        <p class="panel-meta">${c.name}${c.page != null ? `, стр. ${c.page}` : ""}</p>
        ${c.headings && c.headings.length > 0 && html`<p class="muted small">${c.headings.join(" › ")}</p>`}
        <div class="chunk-text">${c.text}</div>
        <div class="row panel-actions">
          <a class="button" href=${link("documents", doc, "view") + "?chunk=" + Number(idx)}>Открыть в документе</a>
          ${c.url && html`<a class="button" href=${c.url} download>Скачать исходник</a>`}
        </div>`;
    }
  } else if (target.pageId) {
    body = html`
      <div class="chunk-text">${source.snippet || ""}</div>
      <div class="row panel-actions"><a class="button" href=${link("wiki", "page", target.pageId)}>Открыть страницу wiki</a></div>`;
  } else if (target.entityId) {
    body = html`
      <div class="chunk-text">${source.snippet || ""}</div>
      <div class="row panel-actions"><a class="button" href=${link("graph", target.entityId)}>Показать в графе</a></div>`;
  } else {
    body = html`<div class="chunk-text">${source.snippet || ""}</div>`;
  }
  return html`
    <aside class="panel" aria-label="Источник">
      <div class="panel-head">
        <h3>${source.n ? `Источник ${source.n}` : "Фрагмент"}</h3>
        <button class="ghost" onClick=${onClose} aria-label="Закрыть">✕</button>
      </div>
      ${body}
    </aside>`;
}
