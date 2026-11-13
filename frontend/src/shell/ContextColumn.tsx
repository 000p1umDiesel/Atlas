import type { ReactNode } from "react";

/** Контекстная колонка раздела (список чатов, фильтры и т. п.); на десктопе. */
export function ContextColumn({ children }: { children: ReactNode }) {
  return (
    <aside
      aria-label="Контекст раздела"
      className="hidden h-full w-72 shrink-0 overflow-y-auto border-r border-line bg-bg/60 p-4 md:block"
    >
      {children}
    </aside>
  );
}
