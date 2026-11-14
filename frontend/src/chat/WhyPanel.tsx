import type { ChatMessage } from "../api/chat";
import type { Source } from "../api/types";
import { formatInt, formatScore } from "../lib/format";
import { KIND_LABELS, labelOf, MODES } from "../lib/labels";
import { sourceName, useOpenSource } from "../sources/SourceList";

/** Подпись режима: «Фрагменты», «Граф»…; неизвестный — как есть. */
function modeLabel(mode: string): string {
  return MODES.find((m) => m.value === mode)?.label ?? mode;
}

/**
 * Доля оценки внутри своего вида (0…1): у реранкера, wiki и графа разные шкалы, поэтому
 * сравниваются только источники одного вида. Один источник вида — полная полоса.
 */
export function relativeScores(sources: Source[]): Map<Source, number> {
  const out = new Map<Source, number>();
  const byKind = new Map<string, Source[]>();
  for (const s of sources) {
    if (typeof s.score !== "number" || !Number.isFinite(s.score)) continue;
    byKind.set(s.kind, [...(byKind.get(s.kind) ?? []), s]);
  }
  for (const group of byKind.values()) {
    const scores = group.map((s) => s.score as number);
    const min = Math.min(...scores);
    const max = Math.max(...scores);
    for (const s of group) out.set(s, max > min ? ((s.score as number) - min) / (max - min) : 1);
  }
  return out;
}

function Bar({ share, label }: { share: number; label: string }) {
  const pct = Math.max(0, Math.min(1, share)) * 100;
  return (
    <span role="img" aria-label={label} className="block h-1.5 w-full overflow-hidden rounded-full bg-ink/10">
      <span className="block h-full rounded-full bg-primary" style={{ width: `${Math.max(pct, 4)}%` }} />
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-x-3 gap-y-0.5 sm:grid-cols-[9rem_minmax(0,1fr)]">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

/**
 * «Почему такой ответ»: по какому запросу искали (переформулировка с учётом диалога и
 * перевод), сколько нашёл поиск и сколько вошло в контекст, и по каждому источнику — вид,
 * кто нашёл (в режиме «Всё вместе»), относительная оценка и размер. У ответов, сохранённых до
 * появления этих полей, недостающие строки просто не показываются.
 */
export function WhyPanel({ message, question, id }: { message: ChatMessage; question: string | null; id?: string }) {
  const openSource = useOpenSource();
  const meta = message.meta;
  const sources = message.sources;
  const cited = sources.filter((s) => s.cited).length;
  const rewritten = message.query && question && message.query.trim() !== question.trim() ? message.query : null;
  const alt = meta?.alt_queries.filter((q) => q.trim()) ?? [];
  const scores = relativeScores(sources);
  const hasScores = scores.size > 0;
  const hasTokens = sources.some((s) => typeof s.tokens === "number" && s.tokens > 0);
  const hasVia = sources.some((s) => s.via);
  const budget = meta?.context_budget;
  const used = meta?.context_tokens;

  return (
    <section id={id} aria-label="Почему такой ответ" className="mt-3 space-y-4 rounded-xl bg-bg/60 px-3 py-3 text-sm">
      <dl className="space-y-2">
        <Row label="Режим">{modeLabel(message.mode)}</Row>
        {rewritten && (
          <Row label="Искал по запросу">
            «{rewritten}»<span className="block text-xs text-muted">вопрос переформулирован с учётом предыдущих сообщений</span>
          </Row>
        )}
        {alt.length > 0 && <Row label="И по переводу">{alt.map((q) => `«${q}»`).join(", ")}</Row>}
        {meta?.retrieved !== undefined && (
          <Row label="Найдено">
            {formatInt(meta.retrieved)}, в ответ вошло {formatInt(sources.length)}
            {meta.retrieved > sources.length && (
              <span className="block text-xs text-muted">остальное не поместилось в бюджет контекста</span>
            )}
          </Row>
        )}
        {used !== undefined && budget !== undefined && budget > 0 && (
          <Row label="Контекст">
            <span className="tabular-nums">
              ≈{formatInt(used)} из {formatInt(budget)} токенов
            </span>
            <span className="mt-1 block max-w-60">
              <Bar share={used / budget} label={`Занято ${Math.round((used / budget) * 100)}% бюджета контекста`} />
            </span>
          </Row>
        )}
        {sources.length > 0 && (
          <Row label="Ссылки в ответе">
            {cited > 0 ? `на ${formatInt(cited)} из ${formatInt(sources.length)} источников` : "ответ не сослался ни на один источник"}
          </Row>
        )}
      </dl>

      {sources.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left">
            <caption className="sr-only">Источники ответа</caption>
            <thead className="text-xs text-muted">
              <tr>
                <th className="py-1 pr-2 font-medium">№</th>
                <th className="py-1 pr-2 font-medium">Источник</th>
                {hasScores && <th className="w-28 py-1 pr-2 font-medium">Оценка</th>}
                {hasTokens && <th className="py-1 text-right font-medium">Токены</th>}
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => {
                const share = scores.get(s);
                return (
                  <tr key={`${s.n}:${s.kind}:${s.ref}`} className="border-t border-line align-top">
                    <td className="py-1.5 pr-2">
                      <span
                        title={s.cited ? "ответ сослался" : "был в контексте, ответ не сослался"}
                        className={`grid h-5 min-w-5 place-items-center rounded-md px-1 text-xs font-semibold tabular-nums ${
                          s.cited ? "bg-primary/20 text-primary-ink" : "bg-bg text-muted"
                        }`}
                      >
                        {s.n}
                      </span>
                    </td>
                    <td className="min-w-0 py-1.5 pr-2">
                      <button
                        type="button"
                        onClick={() => openSource(s)}
                        className="max-w-full truncate text-left font-medium underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
                      >
                        {sourceName(s)}
                      </button>
                      <span className="block text-xs text-muted">
                        {labelOf(KIND_LABELS, s.kind)}
                        {hasVia && s.via ? ` · нашёл режим «${modeLabel(s.via)}»` : ""}
                      </span>
                    </td>
                    {hasScores && (
                      <td className="py-1.5 pr-2">
                        {share !== undefined ? (
                          <>
                            <Bar share={share} label={`Оценка ${formatScore(s.score as number)}`} />
                            <span className="mt-0.5 block text-xs text-muted tabular-nums">{formatScore(s.score as number)}</span>
                          </>
                        ) : (
                          <span className="text-xs text-muted">—</span>
                        )}
                      </td>
                    )}
                    {hasTokens && <td className="py-1.5 text-right text-xs text-muted tabular-nums">{s.tokens ? formatInt(s.tokens) : "—"}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
          {hasScores && (
            <p className="mt-2 text-xs text-muted">
              Полоса показывает оценку относительно источников того же вида: у фрагментов, wiki и графа шкалы разные.
            </p>
          )}
        </div>
      )}
      {meta?.retrieved === undefined && (
        <p className="text-xs text-muted">Подробности поиска сохраняются только у ответов, полученных после обновления.</p>
      )}
    </section>
  );
}
