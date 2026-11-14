import { useRouter } from "@tanstack/react-router";
import { ArrowDown, ArrowUp, BookOpen } from "lucide-react";
import type { MouseEvent } from "react";
import type { EntityListItem } from "../api/types";
import { NAME_LINK, TABLE, TD, TH } from "../ui/styles";
import { countOf, formatInt, formatScore } from "../lib/format";
import type { TypeCatalog } from "../lib/typeCatalog";
import { useMediaQuery } from "../lib/useMediaQuery";
import { isPlainClick, RouterAnchor } from "../ui/RouterAnchor";
import type { EntityOrder, EntitySort } from "./search";

const ALIASES_SHOWN = 3;
const NUM = "text-right tabular-nums";

// Таблица — от md, на узком экране — карточки (одна разметка за раз: ссылки не дублируются).
const WIDE = "(min-width: 768px)";

export const entityHref = (id: string) => `/entities/${encodeURIComponent(id)}`;

/** Псевдонимы, кроме самого имени: первые три и «…». */
function aliasText(item: EntityListItem): string {
  const own = item.name.trim().toLowerCase();
  const rest = item.aliases.filter((a) => a.trim() && a.trim().toLowerCase() !== own);
  const shown = rest.slice(0, ALIASES_SHOWN).join(", ");
  return rest.length > ALIASES_SHOWN ? `${shown}, …` : shown;
}

/** Точка цвета типа: блочная (строчный span с одним размером в браузере 0×0), обёртка — flex. */
function TypeDot({ color }: { color: string }) {
  return (
    <span className="flex shrink-0 pt-1.5">
      <span className="block size-2.5 rounded-full" style={{ backgroundColor: color }} aria-hidden="true" />
    </span>
  );
}

function WikiMark({ item }: { item: EntityListItem }) {
  if (!item.has_page) return null;
  const icon = <BookOpen size={16} aria-hidden="true" />;
  return item.slug ? (
    <RouterAnchor
      href={`/wiki/${encodeURIComponent(item.slug)}`}
      aria-label="Страница wiki"
      title="Страница wiki"
      className="inline-grid size-7 place-items-center rounded-lg text-primary-ink hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary"
    >
      {icon}
    </RouterAnchor>
  ) : (
    <span title="Есть страница wiki" className="inline-grid size-7 place-items-center text-muted">
      {icon}
      <span className="sr-only">Есть страница wiki</span>
    </span>
  );
}

type SortProps = {
  /** Действующая сортировка (с учётом значений по умолчанию). */
  sort: EntitySort;
  order: EntityOrder;
  /** Клик по заголовку: та же колонка — сменить порядок, другая — сортировать по ней. */
  onSort: (sort: EntitySort) => void;
};

function SortHeader({ column, label, sort, order, onSort, numeric }: SortProps & {
  column: EntitySort;
  label: string;
  numeric?: boolean;
}) {
  const active = sort === column;
  const Arrow = order === "asc" ? ArrowUp : ArrowDown;
  return (
    <th className={`${TH} ${numeric ? "text-right" : ""}`} aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        onClick={() => onSort(column)}
        className={`inline-flex items-center gap-1 rounded-md hover:text-ink focus-visible:outline-2 focus-visible:outline-primary ${active ? "text-ink" : ""}`}
      >
        {label}
        <Arrow size={13} aria-hidden="true" className={active ? "" : "opacity-0"} />
      </button>
    </th>
  );
}

type Props = SortProps & {
  items: EntityListItem[];
  catalog: TypeCatalog;
  /** Столбец PageRank: только если анализ дал значение хоть одной строке. */
  showPagerank: boolean;
};

/** Строки каталога: таблица с сортировкой по заголовкам, на узком экране — карточки. */
export function EntityTable({ items, catalog, showPagerank, ...sorting }: Props) {
  const wide = useMediaQuery(WIDE);
  const router = useRouter();

  // Клик по строке (мимо ссылок и кнопок) открывает сущность; с модификаторами — как раньше.
  const openRow = (event: MouseEvent, id: string) => {
    if (!isPlainClick(event) || (event.target as HTMLElement).closest("a, button")) return;
    void router.navigate({ href: entityHref(id) });
  };

  if (!wide) {
    return (
      <ul aria-label="Сущности" className="divide-y divide-line">
        {items.map((item) => {
          const aliases = aliasText(item);
          return (
            <li key={item.entity_id} className="flex items-start gap-3 py-3">
              <TypeDot color={catalog.typeColor(item.type)} />
              <div className="min-w-0 flex-1 space-y-0.5">
                <RouterAnchor href={entityHref(item.entity_id)} className={NAME_LINK}>
                  {item.name}
                </RouterAnchor>
                {aliases && <p className="text-xs text-muted [overflow-wrap:anywhere]">{aliases}</p>}
                <p className="text-[13px] text-muted">
                  {catalog.typeLabel(item.type)} · {countOf(item.mention_count, ["упоминание", "упоминания", "упоминаний"])}
                  {" · "}
                  {countOf(item.degree, ["связь", "связи", "связей"])}
                  {item.pagerank !== null && ` · PageRank ${formatScore(item.pagerank)}`}
                </p>
              </div>
              <WikiMark item={item} />
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <div className="-mx-1 overflow-x-auto px-1">
      <table className={TABLE} aria-label="Сущности">
        <thead>
          <tr>
            <SortHeader column="name" label="Имя" {...sorting} />
            <th className={TH}>Тип</th>
            <SortHeader column="mentions" label="Упоминания" numeric {...sorting} />
            <SortHeader column="degree" label="Связи" numeric {...sorting} />
            {showPagerank && <SortHeader column="pagerank" label="PageRank" numeric {...sorting} />}
            <th className={`${TH} w-10 pr-0`}>
              <span className="sr-only">Wiki</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const aliases = aliasText(item);
            return (
              <tr
                key={item.entity_id}
                onClick={(event) => openRow(event, item.entity_id)}
                className="cursor-pointer transition-colors hover:bg-ink/[0.04]"
              >
                <td className={TD}>
                  <div className="flex items-start gap-2">
                    <TypeDot color={catalog.typeColor(item.type)} />
                    <div className="min-w-0">
                      <RouterAnchor href={entityHref(item.entity_id)} className={NAME_LINK}>
                        {item.name}
                      </RouterAnchor>
                      {aliases && <p className="mt-0.5 text-xs text-muted [overflow-wrap:anywhere]">{aliases}</p>}
                    </div>
                  </div>
                </td>
                <td className={`${TD} text-muted`}>{catalog.typeLabel(item.type)}</td>
                <td className={`${TD} ${NUM}`}>{formatInt(item.mention_count)}</td>
                <td className={`${TD} ${NUM}`}>{formatInt(item.degree)}</td>
                {showPagerank && (
                  <td className={`${TD} ${NUM}`}>{item.pagerank === null ? "—" : formatScore(item.pagerank)}</td>
                )}
                <td className={`${TD} py-1.5 pr-0 text-right`}>
                  <WikiMark item={item} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
