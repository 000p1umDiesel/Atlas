// Сравнение режимов: один вопрос по очереди во всех четырёх режимах, ответы рядом.
// Запросы идут последовательно: так честнее время каждого и меньше нагрузка на LLM.
import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { Markdown } from "../lib/markdown.js";
import { MODES, number, seconds } from "../lib/ui.js";
import { SourceList, sourceTarget } from "./sources.js";

export function Compare({ question, onOpen, onClose }) {
  const [results, setResults] = useState({});
  const top = useRef(null);
  useEffect(() => {
    if (top.current) top.current.scrollIntoView({ block: "start", behavior: "smooth" });
    let alive = true;
    (async () => {
      for (const m of MODES) {
        if (!alive) return;
        setResults((r) => ({ ...r, [m.id]: { loading: true } }));
        try {
          const answer = await api("/api/ask", { method: "POST", json: { question, mode: m.id } });
          if (alive) setResults((r) => ({ ...r, [m.id]: { answer } }));
        } catch (error) {
          if (alive) setResults((r) => ({ ...r, [m.id]: { error } }));
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [question]);

  return html`
    <section class="compare" ref=${top}>
      <header class="row">
        <h2>Сравнение режимов</h2>
        <span class="spacer"></span>
        <button class="ghost" onClick=${onClose}>Скрыть</button>
      </header>
      <p class="compare-question">${question}</p>
      <div class="compare-grid">
        ${MODES.map((m) => {
          const r = results[m.id] || {};
          const a = r.answer;
          const openRef = (ref) => {
            const s = a && a.sources.find((x) => x.n === ref.n);
            if (s) onOpen(sourceTarget(s));
          };
          return html`
            <article class="compare-card">
              <h3>${m.label}</h3>
              ${!r.loading && !a && !r.error && html`<p class="muted small">В очереди</p>`}
              ${r.loading && html`<p class="muted small thinking">Отвечаю…</p>`}
              ${r.error && html`<p class="note note-error">${r.error.detail}</p>`}
              ${a &&
              html`<p class="muted small">${seconds(a.latency_ms)} · ${number(a.tokens_in)} + ${number(a.tokens_out)} токенов</p>
                <${Markdown} text=${a.text} env=${{ citations: true }} onRef=${openRef} />
                <${SourceList} sources=${a.sources} onOpen=${onOpen} />`}
            </article>`;
        })}
      </div>
    </section>`;
}
