// Чат: диалоги, вопрос с выбором режима, ответ потоком с номерами источников.
import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api, streamSse } from "../lib/api.js";
import { Markdown } from "../lib/markdown.js";
import { link, navigate } from "../lib/router.js";
import { ErrorNote, Loading, MODES, number, remember, seconds } from "../lib/ui.js";
import { Compare } from "./compare.js";
import { SourceList, SourcePanel, sourceTarget } from "./sources.js";

export function ConversationList({ chats, activeId }) {
  return html`
    <div class="chat-list">
      <a class="button new-chat" href="#/chat">Новый диалог</a>
      ${chats === null
        ? html`<${Loading} />`
        : chats.length === 0
          ? html`<p class="muted small">Диалогов пока нет.</p>`
          : html`<ul>
              ${chats.map(
                (c) => html`<li><a href=${link("chat", c.id)} class=${c.id === activeId ? "active" : ""}>${c.title || "Без названия"}</a></li>`,
              )}
            </ul>`}
    </div>`;
}

function Metrics({ meta }) {
  if (!meta) return null;
  const tokens = (meta.tokens_in || 0) + (meta.tokens_out || 0);
  return html`<p class="metrics muted small" title="Время ответа и токены LLM (вход + выход)">
    ${seconds(meta.latency_ms || 0)} · ${number(meta.tokens_in)} + ${number(meta.tokens_out)} = ${number(tokens)} токенов
  </p>`;
}

function Answer({ message, onOpen }) {
  const openRef = (ref) => {
    const source = (message.sources || []).find((s) => s.n === ref.n);
    if (source) onOpen(sourceTarget(source));
  };
  if (message.status === "error")
    return html`<article class="msg msg-answer"><p class="note note-error">${message.content}</p></article>`;
  return html`
    <article class="msg msg-answer" aria-busy=${message.streaming ? "true" : "false"}>
      ${message.streaming && !message.content
        ? html`<p class="muted thinking">Ищу в документах и пишу ответ…</p>`
        : html`<${Markdown} text=${message.content} env=${{ citations: true }} onRef=${openRef} />`}
      ${!message.streaming && html`<${SourceList} sources=${message.sources} onOpen=${onOpen} />`}
      ${!message.streaming && html`<${Metrics} meta=${message.meta} />`}
    </article>`;
}

function Question({ message }) {
  const mode = MODES.find((m) => m.id === message.mode);
  return html`
    <div class="msg msg-question">
      <p>${message.content}</p>
      ${mode && message.mode !== "rag" && html`<span class="muted small">режим: ${mode.label}</span>`}
    </div>`;
}

function ModePicker({ mode, setMode, disabled }) {
  return html`
    <div class="modes" role="radiogroup" aria-label="Где искать ответ">
      ${MODES.map(
        (m) => html`<label class=${"mode" + (mode === m.id ? " active" : "")} title=${m.hint}>
          <input type="radio" name="mode" value=${m.id} checked=${mode === m.id} disabled=${disabled}
            onChange=${() => setMode(m.id)} />${m.label}</label>`,
      )}
    </div>`;
}

function ChatHeader({ chat, onRenamed, onDeleted }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [error, setError] = useState(null);
  if (!chat) return null;
  const save = async (event) => {
    event.preventDefault();
    try {
      await api("/api/conversations/" + chat.id, { method: "PATCH", json: { title } });
      setEditing(false);
      onRenamed();
    } catch (err) {
      setError(err);
    }
  };
  const remove = async () => {
    if (!confirm(`Удалить диалог «${chat.title || "Без названия"}» со всеми сообщениями?`)) return;
    try {
      await api("/api/conversations/" + chat.id, { method: "DELETE" });
      onDeleted();
    } catch (err) {
      setError(err);
    }
  };
  return html`
    <header class="chat-head">
      ${editing
        ? html`<form class="row grow" onSubmit=${save}>
            <input value=${title} maxlength="200" aria-label="Название диалога" onInput=${(e) => setTitle(e.currentTarget.value)} />
            <button class="primary" type="submit">Сохранить</button>
            <button type="button" class="ghost" onClick=${() => setEditing(false)}>Отмена</button>
          </form>`
        : html`<h1 class="chat-title">${chat.title || "Без названия"}</h1>
            <div class="row">
              <button class="ghost" onClick=${() => { setTitle(chat.title || ""); setEditing(true); }}>Переименовать</button>
              <button class="ghost danger" onClick=${remove}>Удалить</button>
            </div>`}
      <${ErrorNote} error=${error} />
    </header>`;
}

export function Chat({ id, chats, reloadChats }) {
  const [messages, setMessages] = useState(null);
  const [error, setError] = useState(null);
  const [question, setQuestion] = useState("");
  const [mode, setModeState] = useState(remember("mb.mode") || "rag");
  const [compare, setCompare] = useState(null);
  const [comparing, setComparing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [panel, setPanel] = useState(null);
  const owner = useRef(null); // id диалога, чьи сообщения сейчас в состоянии
  const bottom = useRef(null);
  const setMode = (value) => setModeState(remember("mb.mode", value));

  useEffect(() => {
    setPanel(null);
    setCompare(null);
    if (!id) {
      owner.current = null;
      setMessages([]);
      return;
    }
    if (owner.current === id) return; // только что создан этим же экраном
    owner.current = id;
    setMessages(null);
    api(`/api/conversations/${id}/messages?limit=200`)
      .then((items) => owner.current === id && setMessages(items))
      .catch((err) => {
        setError(err);
        setMessages([]);
      });
  }, [id]);

  useEffect(() => {
    if (bottom.current) bottom.current.scrollIntoView({ block: "end" });
  }, [messages && messages.length]);

  const chat = chats && id ? chats.find((c) => c.id === id) : null;

  const send = async (event) => {
    event.preventDefault();
    const text = question.trim();
    if (!text || busy) return;
    setError(null);
    if (comparing) {
      setCompare({ question: text, key: Date.now() });
      setQuestion("");
      return;
    }
    setBusy(true);
    let cid = id;
    try {
      if (!cid) {
        cid = (await api("/api/conversations", { method: "POST", json: {} })).id;
        owner.current = cid;
        navigate(link("chat", cid));
      }
      setQuestion("");
      const pending = { id: "pending", role: "assistant", content: "", streaming: true, sources: [] };
      setMessages((list) => [...(list || []), { id: "asked", role: "user", content: text, mode }, pending]);
      const update = (patch) =>
        setMessages((list) => list.map((m) => (m.id === "pending" ? { ...m, ...patch(m) } : m)));
      await streamSse(`/api/conversations/${cid}/messages/stream`, { question: text, mode }, (name, data) => {
        if (name === "sources") update(() => ({ sources: data }));
        else if (name === "delta") update((m) => ({ content: m.content + data.text }));
        else if (name === "done") update(() => ({ ...data, streaming: false }));
        else if (name === "error") update(() => ({ status: "error", streaming: false, content: data.detail }));
      });
    } catch (err) {
      setError(err);
      setMessages((list) => (list || []).filter((m) => m.id !== "pending"));
    } finally {
      setBusy(false);
      reloadChats();
    }
  };

  const onKey = (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) send(event);
  };

  return html`
    <div class="chat">
      <div class="chat-scroll sheet">
        <div class="chat-column">
          <${ChatHeader} chat=${chat} onRenamed=${reloadChats}
            onDeleted=${() => { reloadChats(); navigate("#/chat"); }} />
          ${messages === null && html`<${Loading} />`}
          ${messages && messages.length === 0 && !compare &&
          html`<div class="chat-intro">
            <h1>О чём спросить?</h1>
            <p class="muted">Ответ строится по вашим документам. Номера в тексте ведут к источникам — нажмите, чтобы прочитать фрагмент целиком.</p>
          </div>`}
          ${messages &&
          messages.map((m) =>
            m.role === "user"
              ? html`<${Question} key=${m.id} message=${m} />`
              : html`<${Answer} key=${m.id} message=${m} onOpen=${setPanel} />`,
          )}
          ${compare && html`<${Compare} key=${compare.key} question=${compare.question} onOpen=${setPanel} onClose=${() => setCompare(null)} />`}
          <${ErrorNote} error=${error} />
          <div ref=${bottom}></div>
        </div>
      </div>
      <form class="composer" onSubmit=${send}>
        <textarea rows="2" placeholder=${comparing ? "Вопрос для сравнения режимов…" : "Спросите о своих документах…"}
          value=${question} onInput=${(e) => setQuestion(e.currentTarget.value)} onKeyDown=${onKey}
          maxlength="4000" aria-label="Вопрос"></textarea>
        <div class="row composer-bar">
          ${!comparing && html`<${ModePicker} mode=${mode} setMode=${setMode} disabled=${busy} />`}
          <label class="toggle"><input type="checkbox" checked=${comparing} onChange=${(e) => setComparing(e.currentTarget.checked)} /> Сравнить режимы</label>
          <span class="spacer"></span>
          <button class="primary" type="submit" disabled=${busy || !question.trim()}>${busy ? "Отвечаю…" : comparing ? "Сравнить" : "Спросить"}</button>
        </div>
      </form>
      <${SourcePanel} target=${panel} onClose=${() => setPanel(null)} />
    </div>`;
}
