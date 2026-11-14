import { useMutation } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { runChecks, type ServiceCheck } from "../api/status";
import { GHOST_BUTTON } from "../ui/styles";
import { CHECK_LABELS, labelOf } from "../lib/labels";
import { FormNote } from "../ui/form";
import { Block, Dot, type Tone } from "./parts";

const toneOf = (c: ServiceCheck): Tone => (c.ok ? (c.warn ? "warn" : "ok") : "fail");

/** «Сервисы»: проверки по кнопке, без автозапуска и опроса. */
export function Checks() {
  const run = useMutation({ mutationFn: runChecks });
  return (
    <Block
      title="Сервисы"
      action={
        <button type="button" className={GHOST_BUTTON} disabled={run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? "Проверяю…" : "Проверить сервисы"}
        </button>
      }
    >
      <p className="text-sm text-muted">
        Подключается к базам и серверам моделей, ответ LLM не запрашивает. Подробности о сбоях — командой{" "}
        <code className="rounded bg-ink/[0.06] px-1 py-0.5 text-[0.85em]">mnogobase doctor</code> на сервере.
      </p>
      {run.error && (
        <div className="mt-3">
          <FormNote tone="error">{run.error instanceof ApiError ? run.error.detail : "Ошибка запроса"}</FormNote>
        </div>
      )}
      {run.data && (
        <ul className="mt-4 space-y-2 text-sm">
          {run.data.map((c) => (
            <li key={c.name} data-state={toneOf(c)} className="flex items-start gap-2.5">
              <Dot tone={toneOf(c)} />
              <span className="font-medium">{labelOf(CHECK_LABELS, c.name)}</span>
              <span className="text-muted [overflow-wrap:anywhere]">{(c.ok && c.warn ? "внимание: " : "") + c.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}
