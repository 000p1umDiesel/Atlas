// Чтение документа по фрагментам (из индекса, а не из исходного файла: загруженный HTML
// или PDF не исполняется). ?chunk=<номер> подсвечивает фрагмент, на который сослался ответ.
import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { link } from "../lib/router.js";
import { ErrorNote, Loading } from "../lib/ui.js";

const PAGE = 50;

export function DocumentView({ id, query }) {
  const target = query.has("chunk") ? Number(query.get("chunk")) : null;
  const [offset, setOffset] = useState(target != null ? Math.floor(target / PAGE) * PAGE : 0);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const marked = useRef(null);

  useEffect(() => {
    if (target != null) setOffset(Math.floor(target / PAGE) * PAGE);
  }, [id, target]);
  useEffect(() => {
    setData(null);
    api(`/api/documents/${id}/chunks?limit=${PAGE}&offset=${offset}`)
      .then(setData)
      .catch(setError);
  }, [id, offset]);
  useEffect(() => {
    if (marked.current) marked.current.scrollIntoView({ block: "center" });
  }, [data]);

  if (error) return html`<section class="page-wide"><${ErrorNote} error=${error} /></section>`;
  if (!data) return html`<section class="page-wide"><${Loading} /></section>`;
  const pager = data.total > PAGE &&
    html`<div class="row pager">
      <button disabled=${offset === 0} onClick=${() => setOffset(Math.max(0, offset - PAGE))}>Назад</button>
      <span class="muted small">фрагменты ${offset + 1}–${Math.min(offset + PAGE, data.total)} из ${data.total}</span>
      <button disabled=${offset + PAGE >= data.total} onClick=${() => setOffset(offset + PAGE)}>Дальше</button>
    </div>`;
  let lastHeading = "";
  return html`
    <div class="docview sheet">
      <div class="docview-column">
        <p class="crumbs"><a href="#/documents">Документы</a> › <a href=${link("documents", id)}>${data.document.name}</a></p>
        <h1>${data.document.title || data.document.name}</h1>
        ${pager}
        ${data.chunks.map((c) => {
          const heading = (c.headings || []).join(" › ");
          const showHeading = heading && heading !== lastHeading;
          lastHeading = heading;
          const hit = c.idx === target;
          return html`<section key=${c.chunk_id} id=${"chunk-" + c.idx} class=${"doc-chunk" + (hit ? " hit" : "")} ref=${hit ? marked : null}>
            ${showHeading && html`<h3>${heading}</h3>`}
            <span class="chunk-mark" title=${"фрагмент " + (c.idx + 1)}>${c.page_start != null ? "с. " + c.page_start : c.idx + 1}</span>
            <p class=${hit ? "marked" : ""}>${c.text}</p>
          </section>`;
        })}
        ${pager}
      </div>
    </div>`;
}
