// Документы: загрузка пачкой с прогрессом, список со статусами, очередь задач.
import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api, uploadFile } from "../lib/api.js";
import { link } from "../lib/router.js";
import { ago, Empty, ErrorNote, Loading, Status, usePoll } from "../lib/ui.js";

const KIND_LABELS = { ingest: "загрузка", remove: "удаление", analyze: "анализ графа" };
let uploadKey = 0;

function Uploader({ onUploaded }) {
  const [items, setItems] = useState([]);
  const [over, setOver] = useState(false);
  const input = useRef(null);
  const add = (files) => {
    const next = [...files].map((file) => ({ key: ++uploadKey, file, status: "waiting", progress: 0 }));
    if (next.length) setItems((list) => [...list, ...next]);
  };
  const patch = (key, change) => setItems((list) => list.map((i) => (i.key === key ? { ...i, ...change } : i)));

  // По одному файлу за раз: медленный диск сервера и общий лимит очереди.
  useEffect(() => {
    if (items.some((i) => i.status === "uploading")) return;
    const next = items.find((i) => i.status === "waiting");
    if (!next) return;
    patch(next.key, { status: "uploading" });
    uploadFile(next.file, (progress) => patch(next.key, { progress }))
      .then(() => {
        patch(next.key, { status: "done", progress: 1 });
        onUploaded();
      })
      .catch((error) => patch(next.key, { status: "error", error: error.detail }));
  }, [items]);

  const drop = (event) => {
    event.preventDefault();
    setOver(false);
    add(event.dataTransfer.files);
  };
  return html`
    <section class="uploader">
      <div class=${"dropzone" + (over ? " over" : "")} onDragOver=${(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave=${() => setOver(false)} onDrop=${drop}>
        <p><strong>Перетащите файлы сюда</strong>${" или "}<button type="button" class="linklike" onClick=${() => input.current.click()}>выберите на диске</button>.</p>
        <p class="muted small">PDF, Word, PowerPoint, Markdown, HTML, картинки и другие форматы из настроек сервера. Файл появится в поиске, когда обработка закончится.</p>
        <input ref=${input} type="file" multiple hidden onChange=${(e) => { add(e.currentTarget.files); e.currentTarget.value = ""; }} />
      </div>
      ${items.length > 0 &&
      html`<ul class="upload-list">
        ${items.map(
          (i) => html`<li key=${i.key} class=${"upload upload-" + i.status}>
            <span class="upload-name">${i.file.name}</span>
            ${i.status === "error"
              ? html`<span class="upload-error">${i.error}</span>`
              : html`<progress max="1" value=${i.progress} aria-label=${"Загрузка " + i.file.name}></progress>`}
            <span class="small muted">${{ waiting: "ждёт", uploading: Math.round(i.progress * 100) + " %", done: "принят", error: "" }[i.status]}</span>
          </li>`,
        )}
      </ul>
      <button class="ghost small" onClick=${() => setItems((list) => list.filter((i) => i.status === "waiting" || i.status === "uploading"))}>Очистить список</button>`}
    </section>`;
}

function JobsTable({ jobs, onRetry }) {
  if (!jobs.length) return html`<p class="muted">Очередь пуста.</p>`;
  return html`
    <table class="table">
      <thead><tr><th>Файл</th><th>Что</th><th>Статус</th><th>Ход</th><th></th></tr></thead>
      <tbody>
        ${jobs.map(
          (j) => html`<tr key=${j.id}>
            <td>${j.doc_id ? html`<a href=${link("documents", j.doc_id)}>${j.name}</a>` : j.name}</td>
            <td>${KIND_LABELS[j.kind] || j.kind}</td>
            <td><${Status} value=${j.status} /></td>
            <td class="small">${j.status === "failed" ? html`<span class="text-error">${j.error}</span>` : j.status === "running" ? j.progress : ago(j.updated_at)}</td>
            <td>${j.status === "failed" && html`<button class="ghost" onClick=${() => onRetry(j.id)}>Повторить</button>`}</td>
          </tr>`,
        )}
      </tbody>
    </table>`;
}

export function Documents() {
  const [docs, setDocs] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState(null);
  const load = async () => {
    try {
      const [d, j] = await Promise.all([api("/api/documents?limit=200"), api("/api/jobs?limit=30")]);
      setDocs(d);
      setJobs(j);
      setError(null);
    } catch (err) {
      setError(err);
    }
  };
  useEffect(() => {
    load();
  }, []);
  const busy = jobs.some((j) => j.status === "pending" || j.status === "running");
  usePoll(load, 3000, busy);

  const remove = async (doc) => {
    if (!confirm(`Удалить «${doc.name}» из базы? Ответы перестанут на него ссылаться.`)) return;
    try {
      await api("/api/documents/" + doc.id, { method: "DELETE" });
      load();
    } catch (err) {
      setError(err);
    }
  };
  const retry = async (id) => {
    try {
      await api(`/api/jobs/${id}/retry`, { method: "POST" });
      load();
    } catch (err) {
      setError(err);
    }
  };

  return html`
    <section class="page-wide documents">
      <h1>Документы</h1>
      <${Uploader} onUploaded=${load} />
      <${ErrorNote} error=${error} />
      <div class="split">
        <div>
          <h2>В базе</h2>
          ${docs === null
            ? html`<${Loading} />`
            : docs.length === 0
              ? html`<${Empty}>Здесь пока пусто. Загрузите первый документ — после обработки по нему можно будет задавать вопросы.<//>`
              : html`<table class="table">
                  <thead><tr><th>Название</th><th>Статус</th><th></th></tr></thead>
                  <tbody>
                    ${docs.map(
                      (d) => html`<tr key=${d.id + d.name}>
                        <td><a href=${link("documents", d.id)}>${d.name}</a></td>
                        <td><${Status} value=${d.status} /></td>
                        <td class="num"><button class="ghost danger" onClick=${() => remove(d)}>Удалить</button></td>
                      </tr>`,
                    )}
                  </tbody>
                </table>`}
        </div>
        <div>
          <h2>Очередь${busy ? html` <span class="muted small">обновляется</span>` : ""}</h2>
          <${JobsTable} jobs=${jobs} onRetry=${retry} />
        </div>
      </div>
    </section>`;
}
