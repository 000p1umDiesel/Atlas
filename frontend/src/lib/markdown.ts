// Единственный путь недоверенного HTML в страницу: Markdown → markdown-it (без HTML) →
// DOMPurify → ui/Markdown.tsx. Ответы LLM и страницы wiki — недоверенные данные (их может
// написать prompt injection): сырой HTML остаётся видимым текстом, картинок нет, ссылки —
// только http(s), якоря и внутренние пути (/api/, /wiki/, /documents/).
import DOMPurify from "dompurify";
import MarkdownIt, { type StateCore, type Token } from "markdown-it";

export type MarkdownEnv = {
  /** `[n]` — ссылка на источник ответа n (`#cite:n`). */
  citations?: boolean;
  /** `[^<chunk_id>]` — сноска wiki: `chunk_id` → номер; сноска не из карты убирается. */
  footnotes?: Map<string, number>;
};

/** Разрешённые адреса ссылок (и для DOMPurify, и для ссылок markdown-it). */
export const ALLOWED_URI = /^(?:https?:|#|\/api\/|\/wiki\/|\/documents\/)/i;

const md = new MarkdownIt({ html: false, linkify: true, breaks: false });
md.disable("image");
// Ссылку с чужой схемой (`javascript:`, `data:`) markdown-it по умолчанию оставляет сырым
// текстом «[x](javascript:…)». Здесь она разбирается как ссылка, а правило ниже снимает
// с неё <a>: остаётся только подпись. DOMPurify всё равно проверяет href ещё раз.
md.validateLink = () => true;

// Ссылки mnogobase внутри текста:
//   [[slug|подпись]]  — страница wiki (всегда);
//   [^<chunk_id>]     — сноска страницы wiki на фрагмент (только с env.footnotes);
//   [n]               — номер источника ответа (только с env.citations).
const REFS = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|\[\^([0-9a-f]{16}:\d{5})\]|\[(\d{1,3})\]/g;
const FOOTNOTE = /\[\^([0-9a-f]{16}:\d{5})\]/g;

function textToken(state: StateCore, content: string): Token {
  const token = new state.Token("text", "", 0);
  token.content = content;
  return token;
}

function linkTokens(state: StateCore, href: string, label: string, cls: string): Token[] {
  const open = new state.Token("link_open", "a", 1);
  open.attrs = [
    ["href", href],
    ["class", cls],
  ];
  const close = new state.Token("link_close", "a", -1);
  return [open, textToken(state, label), close];
}

/** Токены вместо найденной ссылки mnogobase; `null` — оставить текст как есть. */
function refTokens(state: StateCore, m: RegExpExecArray, env: MarkdownEnv): Token[] | null {
  const [, slug, label, chunkId, n] = m;
  if (slug) {
    const name = slug.trim();
    return linkTokens(state, `/wiki/${encodeURIComponent(name)}`, (label ?? name).trim(), "wikilink");
  }
  if (chunkId && env.footnotes) {
    const number = env.footnotes.get(chunkId);
    return number ? linkTokens(state, `#chunk:${chunkId}`, String(number), "cite") : [];
  }
  if (n && env.citations) return linkTokens(state, `#cite:${n}`, n, "cite");
  return null;
}

function expandRefs(state: StateCore, token: Token, env: MarkdownEnv, out: Token[]): void {
  const text = token.content;
  let last = 0;
  REFS.lastIndex = 0;
  for (let m = REFS.exec(text); m; m = REFS.exec(text)) {
    const made = refTokens(state, m, env);
    if (made === null) continue;
    if (m.index > last) out.push(textToken(state, text.slice(last, m.index)));
    out.push(...made);
    last = m.index + m[0].length;
  }
  if (last === 0) out.push(token);
  else if (last < text.length) out.push(textToken(state, text.slice(last)));
}

md.core.ruler.push("mnogobase_refs", (state) => {
  const env = (state.env ?? {}) as MarkdownEnv;
  for (const block of state.tokens) {
    if (block.type !== "inline" || !block.children) continue;
    const out: Token[] = [];
    // Для каждой открытой ссылки: оставлена ли она (адрес разрешён).
    const links: boolean[] = [];
    for (const token of block.children) {
      if (token.type === "link_open") {
        const keep = ALLOWED_URI.test(String(token.attrGet("href") ?? ""));
        links.push(keep);
        if (keep) out.push(token);
      } else if (token.type === "link_close") {
        if (links.pop()) out.push(token);
      } else if (token.type === "text" && links.length === 0) {
        // Внутри ссылки правило не действует: ссылка в ссылке не нужна.
        expandRefs(state, token, env, out);
      } else {
        out.push(token);
      }
    }
    block.children = out;
  }
});

// Свой экземпляр DOMPurify: хук ниже не касается других пользователей библиотеки.
const purify = DOMPurify(window);
purify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && /^https?:/i.test(node.getAttribute("href") ?? "")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const PURIFY = {
  ALLOWED_URI_REGEXP: ALLOWED_URI,
  FORBID_TAGS: ["style", "img", "svg", "math", "form", "input", "iframe"],
  FORBID_ATTR: ["style"],
  ADD_ATTR: ["target"],
};

/** HTML из Markdown, уже очищенный DOMPurify. */
export function renderMarkdown(text: string, env: MarkdownEnv = {}): string {
  return purify.sanitize(md.render(text || "", env), PURIFY);
}

/**
 * Markdown страницы wiki для показа: без YAML-фронтматтера, без первого заголовка `# `
 * (заголовок страница рисует сама), без раздела `## Sources` и строк определений сносок.
 * Сноски нумеруются по первому появлению в тексте.
 */
export function prepareWikiPage(content: string): { body: string; footnotes: Map<string, number> } {
  let body = content.replace(/^---\n[\s\S]*?\n---\n/, "").trimStart();
  body = body.replace(/^# .*\n+/, "");
  body = body.split(/^## Sources\s*$/m)[0]!;
  body = body.replace(/^\[\^[^\]]+\]:.*$/gm, "").trimEnd();
  const footnotes = new Map<string, number>();
  for (const m of body.matchAll(FOOTNOTE)) if (!footnotes.has(m[1]!)) footnotes.set(m[1]!, footnotes.size + 1);
  return { body, footnotes };
}
