import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FocusEvent,
  type PointerEvent,
  type ReactElement,
} from "react";
import { createPortal } from "react-dom";
import type { TypeInfo } from "../lib/typeCatalog";

/** Задержка показа при наведении мышью: подсказки не мелькают, когда курсор идёт через чипы. */
const HOVER_DELAY_MS = 350;
/** Подсказка только что закрылась — соседняя открывается сразу (как у системных подсказок). */
const WARM_MS = 400;
const GAP = 8;
const MARGIN = 8;

let lastClosed = Number.NEGATIVE_INFINITY;

/** То, что подсказка вешает на кнопку-триггер. */
export interface HintTrigger {
  ref(el: HTMLElement | null): void;
  "aria-describedby"?: string;
  onPointerEnter?(e: PointerEvent<HTMLElement>): void;
  onPointerLeave?(e: PointerEvent<HTMLElement>): void;
  onFocus?(e: FocusEvent<HTMLElement>): void;
  onBlur?(e: FocusEvent<HTMLElement>): void;
  onClick?(): void;
}

type Props = {
  info: TypeInfo;
  /** CSS-цвет типа (точка в подсказке). */
  color: string;
  /** Где показывать, если хватает места: над кнопкой или под ней. */
  placement?: "top" | "bottom";
  /** Нажатие открывает и закрывает подсказку (кнопка «i»: на сенсорном экране нет наведения). */
  toggleOnClick?: boolean;
  /** Обработчики триггера, которые подсказка должна вызвать вместе со своими. */
  onFocus?(): void;
  onBlur?(): void;
  children(trigger: HintTrigger): ReactElement;
};

function focusVisible(el: Element): boolean {
  try {
    return el.matches(":focus-visible");
  } catch {
    return true;
  }
}

/**
 * Немодальная подсказка с определением типа сущности: наведение мышью (с задержкой) или фокус
 * с клавиатуры. Не Radix и не `<style>`: позиция ставится через CSSOM, слой — портал в body.
 * Узел подсказки есть в DOM всегда (скрыт атрибутом `hidden`), чтобы `aria-describedby`
 * читался экранным диктором и без наведения. Определение — только текстовый узел React.
 */
export function TypeHint({ info, color, placement = "bottom", toggleOnClick = false, onFocus, onBlur, children }: Props) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLElement | null>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const show = useCallback(() => {
    clearTimeout(timer.current);
    setOpen(true);
  }, []);
  const hide = useCallback(() => {
    clearTimeout(timer.current);
    setOpen((was) => {
      if (was) lastClosed = performance.now();
      return false;
    });
  }, []);

  useEffect(() => () => clearTimeout(timer.current), []);

  // Рядом с кнопкой, в пределах окна; не хватает места с нужной стороны — с другой.
  const place = useCallback(() => {
    const tip = tipRef.current;
    const trigger = triggerRef.current;
    if (!tip || !trigger) return;
    const r = trigger.getBoundingClientRect();
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = window.innerHeight;
    // Кнопку прокрутили за край окна — подсказка без неё не нужна.
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) {
      hide();
      return;
    }
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const above = r.top - GAP - h;
    const below = r.bottom + GAP;
    const fitsAbove = above >= MARGIN;
    const fitsBelow = below + h <= vh - MARGIN;
    const top = placement === "top" ? (fitsAbove || !fitsBelow ? above : below) : fitsBelow || !fitsAbove ? below : above;
    const left = Math.min(Math.max(MARGIN, r.left), Math.max(MARGIN, vw - MARGIN - w));
    tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(Math.max(MARGIN, top))}px)`;
  }, [placement, hide]);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  // Esc закрывает только подсказку: слушатель на фазе перехвата, раньше страничных, и
  // `preventDefault()` — горячие клавиши графа и панели такой Esc пропускают (не снимают
  // выделение, не закрывают карточку). Прокрутка (фокус с клавиатуры сам прокручивает список)
  // и смена размера окна — переставляют подсказку следом за кнопкой.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      hide();
    };
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, hide, place]);

  const setTrigger = useCallback((el: HTMLElement | null) => {
    triggerRef.current = el;
  }, []);

  // Без определения подсказки нет: триггер получает только свои обработчики фокуса.
  if (!info.description) return children({ ref: setTrigger, onFocus: onFocus && (() => onFocus()), onBlur: onBlur && (() => onBlur()) });

  const trigger: HintTrigger = {
    ref: setTrigger,
    "aria-describedby": id,
    onPointerEnter: (e) => {
      if (e.pointerType === "touch") return;
      clearTimeout(timer.current);
      if (performance.now() - lastClosed < WARM_MS) show();
      else timer.current = setTimeout(show, HOVER_DELAY_MS);
    },
    onPointerLeave: (e) => {
      if (e.pointerType === "touch") return;
      hide();
    },
    onFocus: (e) => {
      onFocus?.();
      if (focusVisible(e.currentTarget)) show();
    },
    onBlur: () => {
      onBlur?.();
      hide();
    },
    onClick: toggleOnClick ? () => (open ? hide() : show()) : undefined,
  };

  return (
    <>
      {children(trigger)}
      {createPortal(
        <div
          ref={tipRef}
          id={id}
          role="tooltip"
          hidden={!open}
          className="type-hint pointer-events-none fixed top-0 left-0 z-[70] w-max max-w-[min(18rem,calc(100vw-16px))] rounded-xl border border-line bg-card px-3 py-2.5 shadow-card"
        >
          <span className="flex items-center gap-1.5 text-[13px] leading-snug font-semibold text-ink">
            <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: color }} aria-hidden="true" />
            {info.label}
          </span>
          <span className="mt-1 block font-serif text-[13px] leading-[1.5] text-muted">{info.description}</span>
        </div>,
        document.body,
      )}
    </>
  );
}
