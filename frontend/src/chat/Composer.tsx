import { ArrowUp } from "lucide-react";
import { useId, useState, type FormEvent, type KeyboardEvent } from "react";
import { MODES, type Mode } from "../lib/labels";

/** Предел длины вопроса на сервере (`MAX_QUESTION_CHARS`). */
export const MAX_QUESTION_CHARS = 16000;

const MODE_KEY = "mb.mode";

/** Режим из localStorage; недоступное хранилище или мусор — `rag`. */
export function loadMode(): Mode {
  try {
    const value = localStorage.getItem(MODE_KEY);
    if (MODES.some((m) => m.value === value)) return value as Mode;
  } catch {
    // хранилище может быть недоступно (приватный режим, блокировка)
  }
  return "rag";
}

function saveMode(mode: Mode): void {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    // выбор просто не переживёт перезагрузку
  }
}

type Props = {
  /**
   * Отправить вопрос (уже без пробелов по краям) в выбранном режиме. `accepted` очищает
   * поле: вызвать, когда сервер принял вопрос (ошибка до этого не должна стереть вопрос).
   */
  onSend: (question: string, mode: Mode, accepted: () => void) => void;
  /** Ответ ещё идёт: отправка и смена режима недоступны. */
  busy?: boolean;
  /** Начальный текст поля (вопрос, который сервер не принял). */
  initialText?: string;
  /** Сравнить режимы: вопрос уходит сюда, а не в `onSend`. Без него флажка нет. */
  onCompare?: (question: string) => void;
};

/** Поле вопроса и выбор режима. Enter — отправить, Shift+Enter — новая строка. */
export function Composer({ onSend, busy = false, initialText = "", onCompare }: Props) {
  const [text, setText] = useState(initialText);
  const [mode, setMode] = useState<Mode>(loadMode);
  const [compare, setCompare] = useState(false);
  const name = useId();
  const question = text.trim();
  const canSend = !busy && question.length > 0;

  const send = () => {
    if (!canSend) return;
    if (compare && onCompare) {
      onCompare(question);
      setText("");
      return;
    }
    onSend(question, mode, () => setText(""));
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    send();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    // Enter, которым подтверждают IME-набор (японский, китайский…), — не отправка.
    // 229 — так Safari помечает такие нажатия.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    send();
  };

  const choose = (next: Mode) => {
    setMode(next);
    saveMode(next);
  };

  return (
    <form onSubmit={onSubmit} className="space-y-2">
      <div className="flex flex-wrap items-center gap-1">
        {!compare && (
          <div role="radiogroup" aria-label="Где искать ответ" className="flex flex-wrap gap-1">
          {MODES.map((m) => (
            <label
              key={m.value}
              title={m.hint}
              className="cursor-pointer rounded-lg px-2.5 py-1 text-xs font-medium text-muted transition-colors select-none hover:text-ink has-checked:bg-primary/20 has-checked:text-primary-ink has-disabled:cursor-not-allowed has-disabled:opacity-60 has-focus-visible:outline-2 has-focus-visible:outline-primary"
            >
              <input
                type="radio"
                name={name}
                value={m.value}
                checked={mode === m.value}
                disabled={busy}
                onChange={() => choose(m.value)}
                className="sr-only"
              />
              {m.label}
            </label>
          ))}
          </div>
        )}
        {onCompare && (
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium text-muted select-none hover:text-ink has-disabled:cursor-not-allowed has-disabled:opacity-60 has-focus-visible:outline-2 has-focus-visible:outline-primary">
            <input type="checkbox" checked={compare} disabled={busy} onChange={(e) => setCompare(e.target.checked)} />
            Сравнить режимы
          </label>
        )}
      </div>
      <div className="flex items-end gap-2 rounded-[14px] border border-line bg-card p-2 shadow-card focus-within:border-primary">
        <textarea
          rows={2}
          maxLength={MAX_QUESTION_CHARS}
          aria-label="Вопрос"
          placeholder={compare ? "Вопрос для сравнения режимов…" : "Спросите о своих документах…"}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          className="max-h-48 min-h-[2.75rem] min-w-0 flex-1 resize-none bg-transparent px-2 py-1.5 text-base text-ink field-sizing-content placeholder:text-muted focus:outline-none md:text-sm"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-xl bg-primary-deep px-3.5 py-2 text-sm font-medium text-on-primary transition-colors hover:bg-primary-deep/90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Отвечаю…" : compare ? "Сравнить" : "Спросить"}
          {!busy && <ArrowUp size={15} strokeWidth={2.2} aria-hidden="true" />}
        </button>
      </div>
    </form>
  );
}
