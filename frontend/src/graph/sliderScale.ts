/**
 * Шкала ползунков фильтра (вес связи, упоминания): диапазон берётся из данных графа,
 * положение 0..100 переводится в значение квадратично — распределения степенные, и важна
 * левая часть (мелкие значения), а редкий хвост сжат вправо.
 */
export type SliderScale = { max: number; step: number };

/** Максимум и шаг по данным: целые значения (вес — сумма целых сил 1..10) — шаг 1, иначе
 * степень десяти около сотой доли максимума. */
export function sliderScale(values: Iterable<number>): SliderScale {
  let max = 0;
  let integers = true;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v > max) max = v;
    if (!Number.isInteger(v)) integers = false;
  }
  if (integers || max <= 0) return { max, step: 1 };
  return { max, step: 10 ** Math.floor(Math.log10(max / 100)) };
}

/** Знаков после запятой у шага (0.01 → 2): убирает хвосты плавающей точки. */
const decimals = (step: number) => Math.max(0, -Math.floor(Math.log10(step)));

/** Значение → положение ползунка 0..100 (больше максимума — правый край). */
export function toSlider(value: number, { max }: SliderScale): number {
  return max > 0 ? Math.round(Math.sqrt(Math.min(Math.max(value, 0), max) / max) * 100) : 0;
}

/** Положение 0..100 → значение, округлённое к шагу; 100 — ровно максимум из данных. */
export function fromSlider(percent: number, { max, step }: SliderScale): number {
  if (percent >= 100) return max;
  const raw = Math.round((max * (percent / 100) ** 2) / step) * step;
  return Number(raw.toFixed(decimals(step)));
}
