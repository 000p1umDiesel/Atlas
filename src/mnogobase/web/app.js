// Веб-интерфейс mnogobase: каркас, вход, навигация по #/маршрутам.
import { html, render, useEffect, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api, onUnauthorized } from "./lib/api.js";
import { link, useRoute } from "./lib/router.js";
import { Loading } from "./lib/ui.js";
import { Account } from "./views/account.js";
import { Chat, ConversationList } from "./views/chat.js";
import { DocumentCard } from "./views/document.js";
import { Documents } from "./views/documents.js";
import { DocumentView } from "./views/docview.js";
import { Login } from "./views/login.js";
import { Wiki } from "./views/wiki.js";

const NAV = [
  ["chat", "Чат"],
  ["documents", "Документы"],
  ["wiki", "Wiki"],
  ["graph", "Граф"],
  ["status", "Статус"],
];

function Placeholder({ title }) {
  return html`<section class="page-narrow"><h1>${title}</h1><p class="muted">Раздел появится в следующей версии.</p></section>`;
}

function Page({ route, user, chats, reloadChats }) {
  const [section, id] = route.parts;
  switch (section) {
    case "account":
      return html`<${Account} user=${user} />`;
    case "chat":
      return html`<${Chat} id=${id} chats=${chats} reloadChats=${reloadChats} />`;
    case "documents":
      if (id && route.parts[2] === "view") return html`<${DocumentView} id=${id} query=${route.query} />`;
      if (id) return html`<${DocumentCard} id=${id} />`;
      return html`<${Documents} />`;
    case "wiki":
      return html`<${Wiki} parts=${route.parts} />`;
    default:
      return html`<${Placeholder} title=${(NAV.find(([id]) => id === section) || NAV[0])[1]} />`;
  }
}

function Sidebar({ route, user, onLogout, open, onClose, children }) {
  const [section] = route.parts;
  return html`
    <aside class=${"sidebar" + (open ? " open" : "")} aria-label="Навигация">
      <div class="sidebar-head">
        <a class="brand-mark" href="#/chat">mnogobase</a>
        <button class="ghost only-mobile" onClick=${onClose} aria-label="Закрыть меню">✕</button>
      </div>
      <nav class="nav">
        ${NAV.map(
          ([id, label]) => html`<a href=${link(id)} class=${section === id ? "active" : ""} aria-current=${section === id ? "page" : null}>${label}</a>`,
        )}
      </nav>
      <div class="sidebar-slot">${children}</div>
      <div class="sidebar-foot">
        <a href="#/account" class=${section === "account" ? "active" : ""}>${user.username}</a>
        <button class="ghost" onClick=${onLogout}>Выйти</button>
      </div>
    </aside>`;
}

function Shell({ user, onLogout }) {
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  const [chats, setChats] = useState(null);
  const reloadChats = () =>
    api("/api/conversations?limit=200")
      .then(setChats)
      .catch(() => setChats((c) => c || []));
  useEffect(() => {
    reloadChats();
  }, []);
  useEffect(() => setMenu(false), [route]);
  const [section, id] = route.parts;
  return html`
    <div class="shell">
      <header class="topbar only-mobile">
        <button class="ghost" onClick=${() => setMenu(true)} aria-label="Открыть меню">☰</button>
        <a class="brand-mark" href="#/chat">mnogobase</a>
      </header>
      <${Sidebar} route=${route} user=${user} onLogout=${onLogout} open=${menu} onClose=${() => setMenu(false)}>
        ${section === "chat" && html`<${ConversationList} chats=${chats} activeId=${id} />`}
      <//>
      ${menu && html`<div class="scrim only-mobile" onClick=${() => setMenu(false)}></div>`}
      <main class="page" id="main">
        <${Page} route=${route} user=${user} chats=${chats} reloadChats=${reloadChats} />
      </main>
    </div>`;
}

function App() {
  const [user, setUser] = useState(undefined);
  useEffect(() => {
    onUnauthorized(() => setUser(null));
    api("/api/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, []);
  const logout = async () => {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } finally {
      setUser(null);
    }
  };
  if (user === undefined) return html`<${Loading} />`;
  if (user === null) return html`<${Login} onLogin=${setUser} />`;
  return html`<${Shell} user=${user} onLogout=${logout} />`;
}

render(html`<${App} />`, document.getElementById("app"));
