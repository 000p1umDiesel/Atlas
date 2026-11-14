import { useEffect, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";

type Props = {
  /** Исходная кнопка: «Удалить». */
  label: string;
  /** Кнопка подтверждения после первого клика: «Удалить навсегда». */
  confirmLabel: string;
  onConfirm: () => void;
  /** Действие выполняется: кнопки недоступны. */
  busy?: boolean;
  className?: string;
};

const BASE =
  "inline-flex items-center justify-center rounded-xl px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-wait disabled:opacity-60";

/**
 * Двухшаговая кнопка вместо `window.confirm`: первый клик показывает подтверждение и
 * «Отмена», второй выполняет действие. Esc, клик или фокус снаружи — отмена.
 */
export function ConfirmButton({ label, confirmLabel, onConfirm, busy = false, className = "" }: Props) {
  const [armed, setArmed] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const startRef = useRef<HTMLButtonElement>(null);
  // После отмены с клавиатуры или «Отмена» фокус возвращается на исходную кнопку.
  const refocus = useRef(false);

  useEffect(() => {
    if (armed) {
      confirmRef.current?.focus();
      // Клик снаружи. Не blur: Safari не фокусирует кнопку по клику, и blur без relatedTarget
      // пришёл бы раньше клика по самому подтверждению.
      const onDown = (event: PointerEvent) => {
        if (!box.current?.contains(event.target as Node)) setArmed(false);
      };
      document.addEventListener("pointerdown", onDown);
      return () => document.removeEventListener("pointerdown", onDown);
    }
    if (refocus.current) {
      refocus.current = false;
      startRef.current?.focus();
    }
  }, [armed]);

  const cancel = () => {
    refocus.current = true;
    setArmed(false);
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !armed) return;
    event.preventDefault(); // Esc не должен заодно закрыть панель, в которой стоит кнопка
    cancel();
  };

  // Фокус ушёл клавиатурой (Tab) на элемент снаружи.
  const onBlur = (event: FocusEvent) => {
    const next = event.relatedTarget as Node | null;
    if (armed && next && !box.current?.contains(next)) setArmed(false);
  };

  return (
    <span ref={box} className={`inline-flex flex-wrap items-center gap-2 ${className}`} onKeyDown={onKeyDown} onBlur={onBlur}>
      {armed ? (
        <>
          <button
            ref={confirmRef}
            type="button"
            disabled={busy}
            onClick={() => {
              refocus.current = true;
              setArmed(false);
              onConfirm();
            }}
            className={`${BASE} bg-danger text-bg hover:bg-danger/90`}
          >
            {confirmLabel}
          </button>
          <button type="button" onClick={cancel} className={`${BASE} text-muted hover:bg-ink/[0.06] hover:text-ink`}>
            Отмена
          </button>
        </>
      ) : (
        <button
          ref={startRef}
          type="button"
          disabled={busy}
          onClick={() => setArmed(true)}
          className={`${BASE} border border-line text-danger hover:bg-danger/10`}
        >
          {label}
        </button>
      )}
    </span>
  );
}
