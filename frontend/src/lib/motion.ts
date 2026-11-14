/** Пользователь просит поменьше анимации (`prefers-reduced-motion`). */
export function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}
