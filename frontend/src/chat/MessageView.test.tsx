import { act, fireEvent, screen } from "@testing-library/react";
import type { Source } from "../api/types";
import { renderWithRouter } from "../test/router";
import type { ChatMessage } from "../api/chat";
import { MessageView } from "./MessageView";

function msg(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m",
    conversation_id: "c1",
    user_id: "u1",
    role: "assistant",
    content: "",
    mode: "rag",
    sources: [],
    status: "done",
    query: null,
    meta: null,
    created_at: "2026-11-14T10:00:00Z",
    ...over,
  };
}

const WIKI: Source = { n: 2, kind: "wiki", ref: "e42#Раздел", path: "adam-smith.md", page: null, snippet: "", cited: true };

test("вопрос в режиме rag — только текст, без подписи режима", async () => {
  await renderWithRouter(<MessageView message={msg({ role: "user", content: "<b>вопрос</b>" })} onCite={vi.fn()} />);
  expect(screen.getByText("<b>вопрос</b>")).toBeTruthy();
  expect(screen.queryByText(/режим:/)).toBeNull();
});

test("вопрос с неизвестным режимом — без подписи", async () => {
  await renderWithRouter(<MessageView message={msg({ role: "user", content: "вопрос", mode: "x" })} onCite={vi.fn()} />);
  expect(screen.queryByText(/режим:/)).toBeNull();
});

test("поток без текста — «Ищу в документах и пишу ответ…», aria-busy; без источников и метрик", async () => {
  const { container } = await renderWithRouter(
    <MessageView
      message={msg({ streaming: true, sources: [WIKI], meta: { latency_ms: 10, tokens_in: 1, tokens_out: 1, alt_queries: [] } })}
      onCite={vi.fn()}
    />,
  );
  expect(screen.getByText("Ищу в документах и пишу ответ…")).toBeTruthy();
  expect(container.querySelector("[aria-busy='true']")).toBeTruthy();
  expect(screen.queryByRole("link", { name: /adam-smith/ })).toBeNull();
  expect(screen.queryByText(/токенов/)).toBeNull();
});

test("поток с текстом — Markdown, aria-busy остаётся", async () => {
  const { container } = await renderWithRouter(
    <MessageView message={msg({ streaming: true, content: "Частичный **ответ**" })} onCite={vi.fn()} />,
  );
  expect(container.querySelector("strong")!.textContent).toBe("ответ");
  expect(container.querySelector("[aria-busy='true']")).toBeTruthy();
});

test("ошибка — только текст ошибки, без Markdown, источников и метрик", async () => {
  const { container } = await renderWithRouter(
    <MessageView
      message={msg({
        status: "error",
        content: "Не удалось получить ответ. Повторите вопрос.",
        sources: [WIKI],
        meta: { latency_ms: 10, tokens_in: 1, tokens_out: 1, alt_queries: [] },
      })}
      onCite={vi.fn()}
    />,
  );
  expect(screen.getByText("Не удалось получить ответ. Повторите вопрос.")).toBeTruthy();
  expect(container.querySelector(".prose")).toBeNull();
  expect(screen.queryByRole("link", { name: /adam-smith/ })).toBeNull();
  expect(screen.queryByText(/токенов/)).toBeNull();
});

test("старый ответ без meta — без метрик", async () => {
  await renderWithRouter(<MessageView message={msg({ content: "Ответ" })} onCite={vi.fn()} />);
  expect(screen.queryByText(/токенов/)).toBeNull();
});

test("клик по [n] — источник с этим номером; неизвестный или кривой номер игнорируется", async () => {
  const onCite = vi.fn();
  const { container } = await renderWithRouter(
    <MessageView message={msg({ content: "Смит [2] и [7].", sources: [WIKI] })} onCite={onCite} />,
  );
  const cites = container.querySelectorAll<HTMLAnchorElement>("a.cite");
  expect(cites).toHaveLength(2);
  await act(async () => fireEvent.click(cites[0]!));
  expect(onCite).toHaveBeenCalledWith(WIKI);
  await act(async () => fireEvent.click(cites[1]!));
  expect(onCite).toHaveBeenCalledTimes(1);
});

test("кривая ссылка #cite:abc (NaN) не ломает поиск источника", async () => {
  const onCite = vi.fn();
  const { container } = await renderWithRouter(
    <MessageView message={msg({ content: "[x](#cite:abc)", sources: [WIKI] })} onCite={onCite} />,
  );
  await act(async () => fireEvent.click(container.querySelector("a")!));
  expect(onCite).not.toHaveBeenCalled();
});
