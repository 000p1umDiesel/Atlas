import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "fail";

const DOT: Record<Tone, string> = { ok: "bg-primary", warn: "bg-amber-500", fail: "bg-danger" };

/** Цветная точка состояния: смысл всегда дублируется текстом рядом. */
export function Dot({ tone }: { tone: Tone }) {
  return <span aria-hidden="true" className={`mt-px inline-block size-2.5 shrink-0 rounded-full ${DOT[tone]}`} />;
}

/** Раздел страницы статуса: заголовок, справа действие. */
export function Block({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}
