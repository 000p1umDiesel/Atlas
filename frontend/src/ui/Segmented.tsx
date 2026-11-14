import { motion } from "motion/react";
import { useId, useRef, type KeyboardEvent } from "react";

type Option<T extends string> = { value: T; label: string };

type Props<T extends string> = {
  label: string;
  options: Option<T>[];
  value: T;
  onChange(value: T): void;
  /**
   * Имя «пилюли»: она перетекает между вариантами (layout-анимация). К нему добавляется id
   * экземпляра: две копии одного переключателя (колонка на десктопе и меню на телефоне) не
   * делят общий layoutId — иначе motion ждёт перехода между ними и не убирает уходящую копию.
   */
  layoutId: string;
  size?: "sm" | "md";
};

/** Сегментный переключатель (radiogroup): стрелки двигают выбор, пилюля перетекает. */
export function Segmented<T extends string>({ label, options, value, onChange, layoutId, size = "md" }: Props<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const index = options.findIndex((o) => o.value === value);
  const pillId = `${layoutId}-${useId()}`;

  const onKeyDown = (event: KeyboardEvent) => {
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const next = (index + step + options.length) % options.length;
    onChange(options[next]!.value);
    refs.current[next]?.focus();
  };

  return (
    <div
      role="radiogroup"
      aria-label={label}
      onKeyDown={onKeyDown}
      className="flex rounded-full bg-ink/[0.06] p-0.5"
    >
      {options.map((o, i) => {
        const checked = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(o.value)}
            className={`relative flex-1 rounded-full font-medium whitespace-nowrap transition-colors focus-visible:outline-2 focus-visible:outline-primary ${
              size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-[13px]"
            } ${checked ? "text-ink" : "text-muted hover:text-ink"}`}
          >
            {checked && (
              <motion.span
                layoutId={pillId}
                // Перетекает только при смене выбора, а не при любом сдвиге раскладки вокруг.
                layoutDependency={value}
                className="absolute inset-0 rounded-full bg-card shadow-card ring-1 ring-line"
                transition={{ type: "spring", stiffness: 500, damping: 38 }}
              />
            )}
            <span className="relative">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}
