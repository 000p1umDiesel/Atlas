// Граф: поиск сущности, её окрестность на холсте (Cytoscape) и карточка выбранного узла.
// Cytoscape (~430 КБ) грузится только на этой странице.
import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { link } from "../lib/router.js";
import { Empty, ErrorNote, Loading } from "../lib/ui.js";
import { SourcePanel } from "./sources.js";

// Cytoscape вставляет свой <style>, если в документе нет элемента с этим id; строгий CSP
// (style-src 'self') такую вставку запрещает. Пустой элемент-заглушка отключает её, а
// контейнер и так позиционирован (.cy { position: absolute }).
const CY_STYLESHEET = "__________cytoscape_stylesheet";

function suppressInlineStyle() {
  if (document.getElementById(CY_STYLESHEET)) return;
  const marker = document.createElement("meta");
  marker.id = CY_STYLESHEET;
  document.head.append(marker);
}

const PALETTE = ["--g1", "--g2", "--g3", "--g4", "--g5", "--g6", "--g7", "--g8"];

function colorFor(type, types) {
  if (!types.has(type)) types.set(type, PALETTE[types.size % PALETTE.length]);
  return getComputedStyle(document.documentElement).getPropertyValue(types.get(type)).trim() || "#888";
}

function Search() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    const query = q.trim();
    if (!query) return setResults(null);
    const t = setTimeout(() => {
      api("/api/graph/search?limit=20&q=" + encodeURIComponent(query))
        .then((r) => {
          setResults(r);
          setError(null);
        })
        .catch(setError);
    }, 250);
    return () => clearTimeout(t);
  }, [q]);
  return html`
    <div class="graph-search">
      <input type="search" placeholder="Найти сущность" value=${q} aria-label="Поиск сущности"
        onInput=${(e) => setQ(e.currentTarget.value)} />
      <${ErrorNote} error=${error} />
      ${results && results.length === 0 && html`<p class="muted small">Ничего не нашлось.</p>`}
      ${results &&
      html`<ul class="graph-results">
        ${results.map((e) => html`<li key=${e.entity_id}><a href=${link("graph", e.entity_id)}>${e.name}</a> <span class="muted small">${e.type}</span></li>`)}
      </ul>`}
    </div>`;
}

function EntityCard({ id, relations, onEvidence }) {
  const [entity, setEntity] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    setEntity(null);
    setError(null);
    api("/api/graph/" + encodeURIComponent(id)).then(setEntity).catch(setError);
  }, [id]);
  if (error) return html`<${ErrorNote} error=${error} />`;
  if (!entity) return html`<${Loading} />`;
  const mine = relations.filter((r) => r.src_id === id || r.dst_id === id);
  return html`
    <div class="entity-card">
      <p class="muted small">${entity.type}</p>
      <h2>${entity.name}</h2>
      ${entity.aliases.length > 0 && html`<p class="muted small">Также: ${entity.aliases.join(", ")}</p>`}
      ${entity.description && html`<p class="entity-description">${entity.description}</p>`}
      <dl class="facts">
        <dt>Упоминаний</dt><dd>${entity.mention_count}</dd>
        ${entity.pagerank != null && html`<dt>PageRank</dt><dd>${entity.pagerank.toFixed(4)}</dd>`}
        ${entity.community != null && html`<dt>Сообщество</dt><dd>${String(entity.community).replace(/^community:/, "").slice(0, 8)}</dd>`}
      </dl>
      <div class="row">
        <a class="button" href=${link("graph", id)}>Сделать центром</a>
        ${entity.has_page && html`<a class="button" href=${link("wiki", entity.slug)}>Страница wiki</a>`}
      </div>
      ${mine.length > 0 &&
      html`<h3>Связи</h3>
        <ul class="relation-list">
          ${mine.map(
            (r, i) => html`<li key=${i}>
              <span>${r.src_name}</span> <span class="predicate">${r.predicate}</span> <span>${r.dst_name}</span>
              ${r.evidence.length > 0 && html` <button class="ghost small" onClick=${() => onEvidence(r.evidence[0])}>фрагмент</button>`}
            </li>`,
          )}
        </ul>`}
    </div>`;
}

export function Graph({ id }) {
  const box = useRef(null);
  const cy = useRef(null);
  const [depth, setDepth] = useState(1);
  const [limit, setLimit] = useState(50);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(id);
  const [panel, setPanel] = useState(null);

  useEffect(() => setSelected(id), [id]);
  useEffect(() => {
    if (!id) return;
    setData(null);
    setError(null);
    api(`/api/graph/${encodeURIComponent(id)}/neighbors?depth=${depth}&limit=${limit}`).then(setData).catch(setError);
  }, [id, depth, limit]);

  useEffect(() => {
    if (!data || !box.current) return undefined;
    let alive = true;
    suppressInlineStyle();
    import("/static/vendor/cytoscape.mjs").then(({ default: cytoscape }) => {
      if (!alive) return;
      const types = new Map();
      const css = getComputedStyle(document.documentElement);
      const ink = css.getPropertyValue("--ink").trim();
      const muted = css.getPropertyValue("--muted").trim();
      const pen = css.getPropertyValue("--pen").trim();
      const paper = css.getPropertyValue("--paper").trim();
      const elements = [
        ...data.nodes.map((n) => ({ data: { id: n.entity_id, label: n.name, color: colorFor(n.type, types), center: n.entity_id === id ? 1 : 0 } })),
        ...data.relations.map((r, i) => ({ data: { id: "r" + i, source: r.src_id, target: r.dst_id, label: r.predicate } })),
      ];
      if (cy.current) cy.current.destroy();
      cy.current = cytoscape({
        container: box.current,
        elements,
        style: [
          { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: ink, "font-family": "Golos Text, sans-serif", "font-size": 12, "text-valign": "bottom", "text-margin-y": 5, width: 16, height: 16, "text-background-color": paper, "text-background-opacity": 0.85, "text-background-padding": 2 } },
          { selector: "node[center = 1]", style: { width: 26, height: 26, "border-width": 3, "border-color": pen, "font-weight": 600 } },
          { selector: "node:selected", style: { "border-width": 3, "border-color": pen } },
          { selector: "edge", style: { width: 1.2, "line-color": muted, "target-arrow-color": muted, "target-arrow-shape": "triangle", "curve-style": "bezier", label: "data(label)", "font-size": 10, color: muted, "text-rotation": "autorotate", "text-background-color": paper, "text-background-opacity": 0.85 } },
        ],
        layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 90, padding: 30 },
      });
      cy.current.on("tap", "node", (event) => setSelected(event.target.id()));
    });
    return () => {
      alive = false;
    };
  }, [data]);
  useEffect(() => () => cy.current && cy.current.destroy(), []);

  return html`
    <div class="graph-page">
      <aside class="graph-side">
        <h1>Граф</h1>
        <${Search} />
        ${id &&
        html`<div class="graph-controls">
          <label>Глубина
            <select value=${depth} onChange=${(e) => setDepth(Number(e.currentTarget.value))}>
              <option value="1">1 шаг</option><option value="2">2 шага</option><option value="3">3 шага</option>
            </select>
          </label>
          <label>Связей не больше
            <select value=${limit} onChange=${(e) => setLimit(Number(e.currentTarget.value))}>
              <option value="50">50</option><option value="100">100</option><option value="200">200</option>
            </select>
          </label>
        </div>`}
      </aside>
      <section class="graph-canvas">
        ${!id && html`<${Empty}>Найдите сущность слева — здесь появятся она и её связи. Узлы можно двигать, колесо мыши меняет масштаб.<//>`}
        ${id && error && html`<${ErrorNote} error=${error} />`}
        ${id && !data && !error && html`<${Loading} />`}
        ${data && data.relations.length === 0 && html`<p class="muted graph-note">У этой сущности пока нет связей.</p>`}
        <div class="cy" ref=${box} aria-label="Граф связей" role="img"></div>
      </section>
      <aside class="graph-card">
        ${selected && data ? html`<${EntityCard} id=${selected} relations=${data.relations} onEvidence=${(chunkId) => setPanel({ chunkId, source: {} })} />` : null}
      </aside>
      <${SourcePanel} target=${panel} onClose=${() => setPanel(null)} />
    </div>`;
}

