/** Визуальный переключатель; роль switch — у кнопки-владельца. */
export function Toggle({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        on ? "bg-primary-deep" : "bg-ink/15"
      }`}
    >
      <span
        className={`absolute size-4 rounded-full bg-card shadow transition-transform ${on ? "translate-x-[18px]" : "translate-x-0.5"}`}
      />
    </span>
  );
}
