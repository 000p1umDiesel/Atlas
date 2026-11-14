import { CircleAlert, CircleCheck } from "lucide-react";
import { useId, type InputHTMLAttributes, type ReactNode } from "react";

/** Основная кнопка формы (заливка primary-deep): читается в обеих темах. */
export const SUBMIT_BUTTON =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-primary-deep px-4 py-2.5 text-sm font-medium text-on-primary transition-colors hover:bg-primary-deep/90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-wait disabled:opacity-70";

type FieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "className"> & { label: string };

/** Поле с подписью над ним; подпись связана с полем (`htmlFor`). */
export function TextField({ label, ...input }: FieldProps) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        className="w-full rounded-xl border border-line bg-bg px-3 py-2.5 text-base text-ink placeholder:text-muted focus-visible:border-primary focus-visible:outline-2 focus-visible:outline-primary md:text-sm"
        {...input}
      />
    </div>
  );
}

/** Сообщение под формой: ошибка (`role=alert`) или успех (`role=status`). Текст — узлом React. */
export function FormNote({ tone, children }: { tone: "error" | "ok"; children: ReactNode }) {
  const Icon = tone === "error" ? CircleAlert : CircleCheck;
  return (
    <p
      role={tone === "error" ? "alert" : "status"}
      className={`flex items-start gap-2 rounded-xl px-3 py-2.5 text-sm ${
        tone === "error" ? "bg-danger/10 text-danger" : "bg-primary/15 text-primary-ink"
      }`}
    >
      <Icon size={16} strokeWidth={2} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </p>
  );
}
