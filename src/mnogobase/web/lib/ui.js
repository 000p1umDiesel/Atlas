// Мелкие общие компоненты и форматирование.
import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";

export function ErrorNote({ error }) {
  if (!error) return null;
  return html`<p class="note note-error" role="alert">${error.detail || String(error)}</p>`;
}

export function Loading({ label = "Загружаю…" }) {
  return html`<p class="muted loading">${label}</p>`;
}

export function Empty({ children }) {
  return html`<div class="empty">${children}</div>`;
}

// Данные с сервера: { data, error, loading, reload }. deps — когда перезагрузить.
export function useLoad(load, deps) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const seq = useRef(0);
  const run = async () => {
    const mine = ++seq.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await load();
      if (mine === seq.current) setState({ data, error: null, loading: false });
    } catch (error) {
      if (mine === seq.current) setState((s) => ({ ...s, error, loading: false }));
    }
  };
  useEffect(() => {
    run();
  }, deps);
  return { ...state, reload: run };
}

// Повтор `fn` каждые `ms`, пока `active` и вкладка видна.
export function usePoll(fn, ms, active) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return undefined;
    const tick = () => {
      if (!document.hidden) saved.current();
    };
    const id = setInterval(tick, ms);
    return () => clearInterval(id);
  }, [ms, active]);
}

const relative = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });

export function ago(seconds) {
  if (seconds == null) return "никогда";
  const diff = seconds - Date.now() / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return relative.format(Math.round(diff), "second");
  if (abs < 3600) return relative.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(diff / 3600), "hour");
  return relative.format(Math.round(diff / 86400), "day");
}

export function seconds(ms) {
  return (ms / 1000).toLocaleString("ru", { maximumFractionDigits: 1 }) + " с";
}

export function number(n) {
  return n == null ? "—" : Number(n).toLocaleString("ru");
}

export const STATUS_LABELS = {
  queued: "в очереди",
  pending: "ждёт",
  running: "в работе",
  processing: "обрабатывается",
  done: "готово",
  failed: "ошибка",
  superseded: "заменена",
};

export function Status({ value }) {
  return html`<span class=${"status status-" + value}>${STATUS_LABELS[value] || value}</span>`;
}

export const MODES = [
  { id: "rag", label: "Фрагменты", hint: "поиск по тексту документов" },
  { id: "wiki", label: "Wiki", hint: "по страницам wiki" },
  { id: "graph", label: "Граф", hint: "по связям сущностей" },
  { id: "all", label: "Всё вместе", hint: "все источники сразу" },
];

export function remember(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch {
    return null;
  }
  return value;
}
