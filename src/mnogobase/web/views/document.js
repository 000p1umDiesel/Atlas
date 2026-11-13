// Карточка документа: стадии обработки с ошибками, фрагменты, сущности.
import { html } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { link, navigate } from "../lib/router.js";
import { ErrorNote, Loading, plural, Status, useLoad } from "../lib/ui.js";

const STAGES = {
  parse: "Разбор файла",
  chunk: "Нарезка на фрагменты",
  embed: "Векторы для поиска",
  extract: "Извлечение сущностей",
  graph: "Запись в граф",
};

export function DocumentCard({ id }) {
  const { data: doc, error, loading } = useLoad(() => api("/api/documents/" + id), [id]);
  if (loading && !doc) return html`<section class="page-wide"><${Loading} /></section>`;
  if (error) return html`<section class="page-wide"><${ErrorNote} error=${error} /><a href="#/documents">К списку документов</a></section>`;
  const remove = async () => {
    if (!confirm(`Удалить «${doc.name}» из базы?`)) return;
    await api("/api/documents/" + doc.id, { method: "DELETE" });
    navigate("#/documents");
  };
  const byType = {};
  for (const e of doc.entities) (byType[e.type] = byType[e.type] || []).push(e);
  return html`
    <section class="page-wide document-card">
      <p class="crumbs"><a href="#/documents">Документы</a></p>
      <h1>${doc.name}</h1>
      <div class="row">
        <${Status} value=${doc.status} />
        <span class="muted">${plural(doc.chunks, ["фрагмент", "фрагмента", "фрагментов"])}, ${plural(doc.entities.length, ["сущность", "сущности", "сущностей"])}</span>
        <span class="spacer"></span>
        ${doc.chunks > 0 && html`<a class="button" href=${link("documents", doc.id, "view")}>Читать</a>`}
        ${doc.url && html`<a class="button" href=${doc.url} download>Скачать</a>`}
        <button class="danger" onClick=${remove}>Удалить</button>
      </div>
      <h2>Обработка</h2>
      ${doc.stages.length === 0
        ? html`<p class="muted">Документ ждёт своей очереди: worker ещё не брал его в работу.</p>`
        : html`<table class="table stages">
            <thead><tr><th>Стадия</th><th>Статус</th><th class="num">Запусков</th><th>Когда</th></tr></thead>
            <tbody>
              ${doc.stages.map(
                (s) => html`<tr key=${s.stage}>
                  <td>${STAGES[s.stage] || s.stage}${s.error && html`<div class="text-error small">${s.error}</div>`}</td>
                  <td><${Status} value=${s.status} /></td>
                  <td class="num">${s.attempts}</td>
                  <td class="small muted">${s.updated_at ? new Date(s.updated_at).toLocaleString("ru") : "—"}</td>
                </tr>`,
              )}
            </tbody>
          </table>`}
      <h2>Сущности</h2>
      ${doc.entities.length === 0
        ? html`<p class="muted">Сущностей пока нет.</p>`
        : Object.keys(byType)
            .sort()
            .map(
              (type) => html`<div class="entity-group" key=${type}>
                <h3>${type}</h3>
                <ul class="entity-list">
                  ${byType[type].map(
                    (e) => html`<li key=${e.entity_id}>
                      <a href=${link("graph", e.entity_id)}>${e.name}</a>
                      ${e.has_page && html` <a class="small" href=${link("wiki", e.slug)} title="Страница wiki">wiki</a>`}
                    </li>`,
                  )}
                </ul>
              </div>`,
            )}
    </section>`;
}

