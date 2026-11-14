import { useStatus } from "../api/status";
import { Analysis } from "../status/Analysis";
import { messageOf } from "../lib/errors";
import { Checks } from "../status/Checks";
import { Usage } from "../status/Usage";
import { Worker } from "../status/Worker";
import { FormNote } from "../ui/form";

/** Статус: очередь и worker, расход LLM, анализ графа, проверки сервисов. */
export function StatusPage() {
  const query = useStatus();
  const status = query.data;
  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 pt-2 pb-10 md:pt-6">
      <h1 className="text-[2.25rem] leading-tight font-semibold tracking-tight">Статус</h1>
      {/* ошибка опроса — над устаревшими данными; при первой загрузке — вместо них */}
      {query.error && <FormNote tone="error">{messageOf(query.error)}</FormNote>}
      {!status && !query.error && <p className="text-muted">Загружаю…</p>}
      {status && (
        <>
          <Worker status={status} />
          <Usage today={status.llm_usage.today} month={status.llm_usage.month} />
          <Analysis />
          <Checks />
        </>
      )}
    </div>
  );
}
