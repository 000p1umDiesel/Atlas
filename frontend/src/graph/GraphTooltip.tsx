import type { Ref } from "react";
import { countOf } from "../lib/format";

export interface TooltipInfo {
  id: string;
  name: string;
  /** Подпись типа (русская, если известна). */
  type: string;
  mentions: number;
  /** CSS-цвет узла (как на холсте). */
  color: string;
}

const MENTIONS: [string, string, string] = ["упоминание", "упоминания", "упоминаний"];

/**
 * Подсказка у курсора над узлом: имя, тип и число упоминаний. Недоверенное имя —
 * только текстовый узел React. Позиция ставится через CSSOM (`placeTooltip`).
 */
export function GraphTooltip({ info, ref }: { info: TooltipInfo; ref?: Ref<HTMLDivElement> }) {
  return (
    <div
      ref={ref}
      role="tooltip"
      className="pointer-events-none absolute top-0 left-0 z-20 max-w-72 rounded-xl border border-line bg-card/95 px-3 py-2 shadow-card backdrop-blur-md"
    >
      <p className="truncate text-[13px] leading-snug font-semibold text-ink">{info.name}</p>
      <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
        <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: info.color }} aria-hidden="true" />
        <span className="truncate">
          {info.type}, {countOf(info.mentions, MENTIONS)}
        </span>
      </p>
    </div>
  );
}

/** Подсказка ниже курсора: справа от узла его подпись, её не закрываем. */
const OFFSET_X = -10;
const OFFSET_Y = 22;

/** Поставить подсказку рядом с курсором, не выходя за контейнер (координаты — client). */
export function placeTooltip(el: HTMLElement | null, container: HTMLElement | null, clientX: number, clientY: number) {
  if (!el || !container) return;
  const rect = container.getBoundingClientRect();
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  let x = clientX - rect.left + OFFSET_X;
  let y = clientY - rect.top + OFFSET_Y;
  if (x + w > rect.width - 4) x = rect.width - 4 - w;
  if (y + h > rect.height - 4) y = clientY - rect.top - OFFSET_Y - h;
  el.style.transform = `translate(${Math.round(Math.max(4, x))}px, ${Math.round(Math.max(4, y))}px)`;
}
