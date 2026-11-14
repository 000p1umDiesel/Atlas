import { act, fireEvent, screen, within } from "@testing-library/react";
import type { ChatMessage } from "../api/chat";
import type { Source } from "../api/types";
import { questionBefore } from "../routes/chat";
import { renderWithRouter } from "../test/router";
import { copyText, type AnswerActionsProps } from "./AnswerActions";
import { MessageView } from "./MessageView";
import { relativeScores } from "./WhyPanel";

function msg(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m",
    conversation_id: "c1",
    user_id: "u1",
    role: "assistant",
    content: "Ответ [1].",
    mode: "rag",
    sources: [],
    status: "done",
    query: null,
    meta: null,
    created_at: "2026-11-14T10:00:00Z",
    ...over,
  };
}

const CHUNK: Source = {
  n: 1,
  kind: "chunk",
  ref: "d1:00003",
  path: "report.pdf",
  page: 4,
  snippet: "",
  cited: true,
  score: 0.9,
  tokens: 120,
  via: "rag",
};
const CHUNK2: Source = { ...CHUNK, n: 2, ref: "d1:00007", page: null, cited: false, score: 0.3, tokens: 80 };
const REL: Source = { n: 3, kind: "relation", ref: "r1", path: null, page: null, snippet: "A uses B", cited: false, score: 2.5, tokens: 10, via: "graph" };

function actions(over: Partial<AnswerActionsProps> = {}): AnswerActionsProps {
  return { question: "Что в отчёте?", busy: false, onAsk: vi.fn(), onCompare: vi.fn(), ...over };
}

test("questionBefore — ближайший вопрос пользователя выше ответа", () => {
  const list = [msg({ role: "user", content: "первый" }), msg({}), msg({ role: "user", content: "второй" }), msg({})];
  expect(questionBefore(list, 1)).toBe("первый");
  expect(questionBefore(list, 3)).toBe("второй");
  expect(questionBefore(list, 0)).toBeNull();
});

test("copyText — Markdown ответа и список процитированных источников", () => {
  expect(copyText(msg({ content: "Без ссылок", sources: [CHUNK2] }))).toBe("Без ссылок");
  expect(copyText(msg({ content: "Ответ [1].\n", sources: [CHUNK, CHUNK2] }))).toBe(
    "Ответ [1].\n\nИсточники:\n[1] report.pdf, стр. 4\n",
  );
});

test("без actions (сравнение режимов) кнопок под ответом нет", async () => {
  await renderWithRouter(<MessageView message={msg({ sources: [CHUNK] })} onCite={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Копировать" })).toBeNull();
});

test("копировать — текст в буфер и «Скопировано»; отказ буфера — понятная надпись", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  await renderWithRouter(<MessageView message={msg({ sources: [CHUNK] })} onCite={vi.fn()} actions={actions()} />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Копировать" })));
  expect(writeText).toHaveBeenCalledWith("Ответ [1].\n\nИсточники:\n[1] report.pdf, стр. 4\n");
  expect(screen.getByRole("button", { name: "Скопировано" })).toBeTruthy();

  writeText.mockRejectedValueOnce(new Error("denied"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Скопировано" })));
  expect(screen.getByRole("button", { name: "Не удалось скопировать" })).toBeTruthy();
});

test("спросить снова и другой режим — тот же вопрос; «Сравнить все режимы»", async () => {
  const a = actions();
  await renderWithRouter(<MessageView message={msg({ mode: "graph" })} onCite={vi.fn()} actions={a} />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Повторить" })));
  expect(a.onAsk).toHaveBeenLastCalledWith("Что в отчёте?", "graph");

  const toggle = screen.getByRole("button", { name: "Другой режим" });
  await act(async () => fireEvent.click(toggle));
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const group = screen.getByRole("group", { name: "Спросить в другом режиме" });
  // Текущий режим не предлагается.
  expect(within(group).queryByRole("button", { name: "Граф" })).toBeNull();
  await act(async () => fireEvent.click(within(group).getByRole("button", { name: "Wiki" })));
  expect(a.onAsk).toHaveBeenLastCalledWith("Что в отчёте?", "wiki");
  expect(screen.queryByRole("group", { name: "Спросить в другом режиме" })).toBeNull();

  await act(async () => fireEvent.click(toggle));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Сравнить все режимы" })));
  expect(a.onCompare).toHaveBeenCalledWith("Что в отчёте?");
});

test("пока идёт другой ответ, отправить нельзя; копировать — можно", async () => {
  await renderWithRouter(<MessageView message={msg({})} onCite={vi.fn()} actions={actions({ busy: true })} />);
  expect((screen.getByRole("button", { name: "Повторить" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Копировать" }) as HTMLButtonElement).disabled).toBe(false);
});

test("ответ-ошибка — только «Повторить»", async () => {
  const a = actions();
  await renderWithRouter(
    <MessageView message={msg({ status: "error", content: "Не удалось получить ответ." })} onCite={vi.fn()} actions={a} />,
  );
  expect(screen.queryByRole("button", { name: "Копировать" })).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Повторить" })));
  expect(a.onAsk).toHaveBeenCalledWith("Что в отчёте?", "rag");
});

test("«Почему такой ответ»: запрос, перевод, найдено/вошло, бюджет и источники", async () => {
  const message = msg({
    mode: "all",
    query: "Что в отчёте Сбера за 2025 год?",
    sources: [CHUNK, CHUNK2, REL],
    meta: {
      latency_ms: 900,
      tokens_in: 10,
      tokens_out: 5,
      alt_queries: ["What is in the report?"],
      retrieved: 7,
      context_tokens: 210,
      context_budget: 20000,
    },
  });
  await renderWithRouter(<MessageView message={message} onCite={vi.fn()} actions={actions()} />);
  const toggle = screen.getByRole("button", { name: "Почему такой ответ" });
  await act(async () => fireEvent.click(toggle));
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const panel = screen.getByRole("region", { name: "Почему такой ответ" });
  expect(within(panel).getByText("«Что в отчёте Сбера за 2025 год?»", { exact: false })).toBeTruthy();
  expect(within(panel).getByText("«What is in the report?»")).toBeTruthy();
  expect(within(panel).getByText(/7, в ответ вошло 3/)).toBeTruthy();
  expect(within(panel).getByText(/остальное не поместилось/)).toBeTruthy();
  expect(within(panel).getByText(/≈210 из/)).toBeTruthy();
  expect(within(panel).getByText("на 1 из 3 источников")).toBeTruthy();
  expect(within(panel).getAllByText(/нашёл режим «Фрагменты»/)).toHaveLength(2);
  expect(within(panel).getByText(/нашёл режим «Граф»/)).toBeTruthy();
});

test("«Почему» у старого ответа — без оценок, с пометкой", async () => {
  const old = { ...CHUNK, score: undefined, tokens: undefined, via: undefined };
  const message = msg({ sources: [old], meta: { latency_ms: 1, tokens_in: 1, tokens_out: 1, alt_queries: [] } });
  await renderWithRouter(<MessageView message={message} onCite={vi.fn()} actions={actions()} />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Почему такой ответ" })));
  const panel = screen.getByRole("region", { name: "Почему такой ответ" });
  expect(within(panel).queryByText("Оценка")).toBeNull();
  expect(within(panel).getByText(/только у ответов, полученных после обновления/)).toBeTruthy();
});

test("relativeScores — нормируется внутри вида; один источник вида — полная полоса", () => {
  const shares = relativeScores([CHUNK, CHUNK2, REL, { ...REL, n: 4, score: null }]);
  expect(shares.get(CHUNK)).toBe(1);
  expect(shares.get(CHUNK2)).toBe(0);
  expect(shares.get(REL)).toBe(1);
  expect(shares.size).toBe(3);
});
