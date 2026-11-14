import { Fragment } from "react";
import type { PathView } from "../api/types";
import { countOf } from "../lib/format";

const STEPS: [string, string, string] = ["шаг", "шага", "шагов"];
/** Наибольшая длина пути, которую принимает сервер. */
const MAX_HOPS = 5;

type Props = {
  paths: PathView[];
  /** Поиск упёрся в таймаут сервера. */
  timedOut: boolean;
  /** Наибольшая длина пути текущего поиска. */
  hops: number;
  /** Закреплённый путь (индекс) — его узлы подсвечены, пока не снимут. */
  pinned: number | null;
  onPin(index: number | null): void;
  /** Наведение или фокус на пути; null — ушли. */
  onHover(index: number | null): void;
  /** Искать пути до наибольшей длины. */
  onWiden(): void;
};

/** Цепочка пути: «A → предикат → B ← предикат ← C»; стрелка — по хранимому направлению связи. */
function Chain({ path }: { path: PathView }) {
  return (
    <span className="mt-0.5 block text-sm leading-snug break-words">
      {path.nodes.map((node, i) => {
        const relation = i > 0 ? path.relations[i - 1] : undefined;
        let link = "";
        if (relation) {
          // Связь хранится от src к dst: совпадает с ходом пути — «→», иначе — «←».
          const arrow = relation.src_id === path.nodes[i - 1]!.entity_id ? "→" : "←";
          link = ` ${arrow} ${relation.predicate} ${arrow} `;
        }
        return (
          <Fragment key={`${i}-${node.entity_id}`}>
            {link && <span className="text-muted">{link}</span>}
            <span className="font-medium">{node.name}</span>
          </Fragment>
        );
      })}
    </span>
  );
}

/**
 * Найденные пути под выбором концов: наведение и фокус подсвечивают узлы пути на графе,
 * клик закрепляет подсветку (повторный — снимает). Пусто — объяснение, почему.
 */
export function PathList({ paths, timedOut, hops, pinned, onPin, onHover, onWiden }: Props) {
  if (paths.length === 0) {
    if (timedOut) return <p className="px-1 text-sm text-muted">Поиск пути занял слишком долго. Уменьшите длину пути.</p>;
    return (
      <div className="space-y-2 px-1">
        <p className="text-sm text-muted">{`Путей длиной до ${hops} ${hops === 1 ? "шага" : "шагов"} не нашлось.`}</p>
        {hops < MAX_HOPS && (
          <button
            type="button"
            onClick={onWiden}
            className="rounded-xl bg-primary-deep px-3 py-1.5 text-[13px] font-medium text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            Искать до {MAX_HOPS} шагов
          </button>
        )}
      </div>
    );
  }

  return (
    <ol aria-label="Найденные пути" className="space-y-0.5">
      {paths.map((path, i) => {
        const pressed = pinned === i;
        return (
          <li key={i}>
            <button
              type="button"
              aria-pressed={pressed}
              onMouseEnter={() => onHover(i)}
              onMouseLeave={() => onHover(null)}
              onFocus={() => onHover(i)}
              onBlur={() => onHover(null)}
              onClick={() => onPin(pressed ? null : i)}
              className="w-full rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary aria-pressed:bg-primary/15"
            >
              <span className="block text-xs font-medium text-muted tabular-nums">
                Путь {i + 1} · {countOf(path.relations.length, STEPS)}
              </span>
              <Chain path={path} />
            </button>
          </li>
        );
      })}
    </ol>
  );
}
