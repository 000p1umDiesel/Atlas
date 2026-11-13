// Единственное место, где HTML попадает в страницу: Markdown → markdown-it (без HTML) →
// DOMPurify → innerHTML. Тексты ответов, wiki и документов — недоверенные данные (их может
// написать prompt injection), поэтому сырой HTML не исполняется, ссылки только http(s),
// якоря и /api/, картинки выключены (CSP их всё равно не пустит).
import { html } from "/static/vendor/htm-preact-standalone.mjs";
import MarkdownIt from "/static/vendor/markdown-it.mjs";
import DOMPurify from "/static/vendor/purify.mjs";

const md = new MarkdownIt({ html: false, linkify: true, breaks: false });
md.disable("image");

// Ссылки mnogobase внутри текста:
//   [n]                — номер источника ответа;
//   [^<chunk_id>]      — сноска страницы wiki на фрагмент документа;
//   [[slug|подпись]]   — ссылка на другую страницу wiki.
const REFS = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|\[\^([0-9a-f]{16}:\d{5})\]|\[(\d{1,3})\]/g;

function linkTokens(state, href, label, cls) {
  const open = new state.Token("link_open", "a", 1);
  open.attrs = [["href", href], ["class", cls]];
  const text = new state.Token("text", "", 0);
  text.content = label;
  const close = new state.Token("link_close", "a", -1);
  return [open, text, close];
}

md.core.ruler.push("mnogobase_refs", (state) => {
  const env = state.env || {};
  for (const block of state.tokens) {
    if (block.type !== "inline" || !block.children) continue;
    const out = [];
    let insideLink = 0;
    for (const token of block.children) {
      if (token.type === "link_open") insideLink += 1;
      if (token.type === "link_close") insideLink -= 1;
      if (token.type !== "text" || insideLink) {
        out.push(token);
        continue;
      }
      let last = 0;
      const text = token.content;
      REFS.lastIndex = 0;
      for (let m; (m = REFS.exec(text)); ) {
        let made = null;
        if (m[1]) {
          made = linkTokens(state, "#/wiki/" + encodeURIComponent(m[1].trim()), (m[2] || m[1]).trim(), "wikilink");
        } else if (m[3] && env.footnotes) {
          const n = env.footnotes.get(m[3]);
          if (n) made = linkTokens(state, "#chunk:" + m[3], String(n), "cite");
          else made = [];
        } else if (m[4] && env.citations) {
          made = linkTokens(state, "#cite:" + m[4], m[4], "cite");
        }
        if (made === null) continue;
        if (m.index > last) {
          const before = new state.Token("text", "", 0);
          before.content = text.slice(last, m.index);
          out.push(before);
        }
        out.push(...made);
        last = m.index + m[0].length;
      }
      if (last === 0) out.push(token);
      else if (last < text.length) {
        const after = new state.Token("text", "", 0);
        after.content = text.slice(last);
        out.push(after);
      }
    }
    block.children = out;
  }
});

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && /^https?:/i.test(node.getAttribute("href") || "")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const PURIFY = {
  ALLOWED_URI_REGEXP: /^(?:https?:|#|\/api\/)/i,
  FORBID_TAGS: ["style", "img", "svg", "math", "form", "input", "iframe"],
  FORBID_ATTR: ["style"],
  ADD_ATTR: ["target"],
};

export function renderMarkdown(text, env = {}) {
  return DOMPurify.sanitize(md.render(text || "", env), PURIFY);
}

// onRef({kind: "cite", n} | {kind: "chunk", id}) — клик по номеру источника или сноске.
export function Markdown({ text, env, onRef, class: cls = "prose" }) {
  const onClick = (event) => {
    const anchor = event.target.closest("a");
    if (!anchor || !onRef) return;
    const href = anchor.getAttribute("href") || "";
    if (href.startsWith("#cite:")) {
      event.preventDefault();
      onRef({ kind: "cite", n: Number(href.slice(6)) });
    } else if (href.startsWith("#chunk:")) {
      event.preventDefault();
      onRef({ kind: "chunk", id: href.slice(7) });
    }
  };
  return html`<div class=${cls} onClick=${onClick} dangerouslySetInnerHTML=${{ __html: renderMarkdown(text, env) }}></div>`;
}
