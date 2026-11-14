import { useRouter } from "@tanstack/react-router";
import { useMemo, type MouseEvent } from "react";
import { renderMarkdown, type MarkdownEnv } from "../lib/markdown";
import { isPlainClick } from "./RouterAnchor";

/** Клик по номеру источника ответа (`[n]`) или по сноске wiki (`[^chunk_id]`). */
export type MarkdownRef = { kind: "cite"; n: number } | { kind: "chunk"; id: string };

type Props = {
  text: string;
  env?: MarkdownEnv;
  onRef?: (ref: MarkdownRef) => void;
  className?: string;
};

/** Внутренние адреса, по которым переходит роутер (без перезагрузки страницы). */
const ROUTED = /^\/(?:wiki|documents)\//;

/**
 * Markdown ответа или страницы wiki. ЕДИНСТВЕННОЕ место, где HTML вставляется в страницу:
 * текст проходит markdown-it без HTML и DOMPurify (`lib/markdown`). Всё остальное
 * недоверенное (вопросы, фрагменты, имена, сниппеты) — текстовыми узлами React.
 */
export function Markdown({ text, env, onRef, className = "prose" }: Props) {
  // Вне роутера (например, в изолированном тесте) внутренние ссылки ведут себя как обычные.
  const router = useRouter({ warn: false }) as ReturnType<typeof useRouter> | undefined;
  const citations = env?.citations;
  const footnotes = env?.footnotes;
  const html = useMemo(() => renderMarkdown(text, { citations, footnotes }), [text, citations, footnotes]);

  // Делегированный клик: ссылки — внутри HTML, своих обработчиков у них нет.
  const onClick = (event: MouseEvent<HTMLDivElement>) => {
    const anchor = (event.target as Element).closest?.("a");
    if (!anchor || !event.currentTarget.contains(anchor)) return;
    const href = anchor.getAttribute("href") ?? "";
    if (href.startsWith("#cite:")) {
      event.preventDefault();
      onRef?.({ kind: "cite", n: Number(href.slice("#cite:".length)) });
    } else if (href.startsWith("#chunk:")) {
      event.preventDefault();
      onRef?.({ kind: "chunk", id: href.slice("#chunk:".length) });
    } else if (router && ROUTED.test(href) && isPlainClick(event)) {
      event.preventDefault();
      void router.navigate({ href });
    }
  };

  // Ссылки внутри HTML доступны с клавиатуры сами; div лишь ловит всплывающий клик.
  return <div className={className} onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}
