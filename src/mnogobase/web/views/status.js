// Статус: очередь и worker, расход LLM, анализ графа, быстрые проверки сервисов.
import { html, useEffect, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { link } from "../lib/router.js";
import { ago, ErrorNote, Loading, number, Status, STATUS_LABELS, usePoll } from "../lib/ui.js";

const CHECK_LABELS = {
  database: "PostgreSQL",
  models: "Серверы моделей (эмбеддинги, реранкер, зрение)",
  llm: "LLM",
  qdrant: "Qdrant (векторы)",
  neo4j: "Neo4j (граф)",
  graph_plugins: "Плагины графа (APOC, GDS)",
};

function Usage({ title, rows }) {
  return html`
    <div>
      <h3>${title}</h3>
      ${rows.length === 0
        ? html`<p class="muted small">Вызовов не было.</p>`
        : html`<table class="table">
            <thead><tr><th>Модель</th><th class="num">Вызовы</th><th class="num">Токены</th><th class="num">Ошибки</th><th class="num">Оценка</th></tr></thead>
            <tbody>
              ${rows.map(
                (r) => html`<tr key=${r.model + r.currency}>
                  <td>${r.model}${r.external ? "" : html` <span class="muted small">локально</span>`}</td>
                  <td class="num">${number(r.calls)}</td>
                  <td class="num">${number((r.tokens_in || 0) + (r.tokens_out || 0))}</td>
                  <td class="num">${number(r.errors)}</td>
                  <td class="num">${r.estimated_cost != null ? `${r.estimated_cost} ${r.currency || ""}` : "—"}</td>
                </tr>`,
              )}
            </tbody>
          </table>`}
    </div>`;
}

function Checks() {
  const [checks, setChecks] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setChecks(await api("/api/status/checks"));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return html`
    <section class="status-block">
      <div class="row"><h2>Сервисы</h2><span class="spacer"></span>
        <button onClick=${run} disabled=${busy}>${busy ? "Проверяю…" : "Проверить сервисы"}</button></div>
      <p class="muted small">Подключается к базам и серверам моделей, ответ LLM не запрашивает. Подробности о сбоях — командой <code>mnogobase doctor</code> на сервере.</p>
      <${ErrorNote} error=${error} />
      ${checks &&
      html`<ul class="checks">
        ${checks.map(
          (c) => html`<li key=${c.name} class=${c.ok ? (c.warn ? "warn" : "ok") : "fail"}>
            <span class="check-dot" aria-hidden="true"></span>
            <span>${CHECK_LABELS[c.name] || c.name}</span>
            <span class="muted small">${c.ok ? (c.warn ? "внимание: " : "") + c.detail : c.detail}</span>
          </li>`,
        )}
      </ul>`}
    </section>`;
}

// Задача анализа приходит вместе со снимком (`job`), а не из списка задач: там её
// вытесняют новые загрузки. Пока она ждёт или идёт, состояние опрашивается.
function Analysis({ reload }) {
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  const load = () =>
    api("/api/graph/analysis")
      .then((data) => {
        setState(data);
        setError(null);
      })
      .catch(setError);
  useEffect(() => {
    load();
  }, []);
  const job = state && state.job;
  const active = Boolean(job && (job.status === "pending" || job.status === "running"));
  usePoll(load, 5000, active);
  const start = async () => {
    setError(null);
    try {
      await api("/api/graph/analyze", { method: "POST" });
      load();
      reload();
    } catch (err) {
      setError(err);
    }
  };
  const meta = (state && state.metadata) || {};
  return html`
    <section class="status-block">
      <div class="row"><h2>Анализ графа</h2><span class="spacer"></span>
        <button onClick=${start} disabled=${active}>${active ? "Анализ идёт…" : "Запустить анализ графа"}</button></div>
      <p class="muted small">PageRank находит самые связанные сущности, Leiden делит граф на сообщества. Ответы в режимах «Граф» и «Всё вместе» используют последний готовый снимок.</p>
      <${ErrorNote} error=${error} />
      ${job && html`<p class="small">Задача: <${Status} value=${job.status} /> ${job.status === "running" ? job.progress : ""}${job.status === "failed" ? html` <span class="text-error">${(job.error || "").replace(/^AnalysisError: /, "")}</span>` : ""}</p>`}
      ${!state && !error && html`<${Loading} />`}
      ${state &&
      (state.ready
        ? html`<p>Снимок от ${new Date(state.completed_at).toLocaleString("ru")}: ${number(meta.nodes)} сущностей, ${number(meta.relationships)} связей, ${number(meta.communities)} сообществ.</p>
            ${state.top.length > 0 &&
            html`<h3>Самые связанные</h3>
              <ol class="top-entities">${state.top.slice(0, 10).map((e) => html`<li key=${e.entity_id}><a href=${link("graph", e.entity_id)}>${e.name}</a> <span class="muted small">${e.pagerank.toFixed(3)}</span></li>`)}</ol>`}`
        : html`<p class="muted">${Object.keys(meta).length ? "Готового снимка нет: граф изменился после прошлого анализа." : "Анализ ещё не запускался."}</p>`)}
    </section>`;
}

export function StatusPage() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const load = async () => {
    try {
      setStatus(await api("/api/status"));
      setError(null);
    } catch (err) {
      setError(err);
    }
  };
  useEffect(() => {
    load();
  }, []);
  usePoll(load, 5000, true);
  if (!status) return html`<section class="page-wide">${error ? html`<${ErrorNote} error=${error} />` : html`<${Loading} />`}</section>`;
  const worker = status.worker;
  const queue = Object.entries(status.queue || {});
  return html`
    <section class="page-wide status-page">
      <h1>Статус</h1>
      <${ErrorNote} error=${error} />
      <section class="status-block">
        <h2>Обработка документов</h2>
        <p class=${"worker " + (worker.alive ? "ok" : "fail")}>
          <span class="check-dot" aria-hidden="true"></span>
          ${worker.alive ? `Worker работает, отмечался ${ago(worker.last_seen)}` : worker.last_seen ? `Worker не отвечает: последний раз ${ago(worker.last_seen)}` : "Worker ещё ни разу не запускался"}
        </p>
        <p class="row small">${queue.length === 0 ? html`<span class="muted">Очередь пуста.</span>` : queue.map(([s, n]) => html`<span key=${s}>${STATUS_LABELS[s] || s}: <strong>${number(n)}</strong></span>`)}</p>
        ${status.running.length > 0 &&
        html`<ul class="running">${status.running.map((j) => html`<li key=${j.id}>${j.name} — ${j.progress || "начинается"}</li>`)}</ul>`}
      </section>
      <section class="status-block">
        <h2>Расход LLM</h2>
        <div class="split">
          <${Usage} title="Сегодня (UTC)" rows=${status.llm_usage.today} />
          <${Usage} title="Этот месяц" rows=${status.llm_usage.month} />
        </div>
      </section>
      <${Analysis} reload=${load} />
      <${Checks} />
    </section>`;
}
