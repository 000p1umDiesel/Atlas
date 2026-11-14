import { useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { apiGet } from "../api/client";
import { useEntity } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import type { EntityListItem, EntityPage } from "../api/types";

/** Пауза после ввода перед запросом, мс. */
const DEBOUNCE_MS = 200;
/** Вариантов в списке. */
const OPTIONS_LIMIT = 8;

type Props = {
  label: string;
  value: string | null;
  onChange(id: string | null): void;
  /** Эта сущность не предлагается (второй конец пути). */
  exclude?: string;
};

/**
 * Выбор сущности по имени (комбобокс): ввод → через 200 мс поиск `GET /api/entities?q=…`,
 * стрелки, Enter, Esc и клик. Показываются только варианты на последний введённый текст;
 * выбранная сущность — её имя в поле.
 */
export function EntityPicker({ label, value, onChange, exclude }: Props) {
  const catalog = useTypeCatalog();
  const selected = useEntity(value);
  const ids = { input: useId(), list: useId() };
  const inputRef = useRef<HTMLInputElement>(null);
  // Текст, который вводят сейчас; null — поле показывает выбранную сущность.
  const [draft, setDraft] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  // Имя только что выбранного варианта: показывается, пока не загрузилась карточка.
  const [picked, setPicked] = useState<{ id: string; name: string } | null>(null);

  const typed = (draft ?? "").trim();
  useEffect(() => {
    const t = setTimeout(() => setQuery(typed), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [typed]);

  const results = useQuery<EntityPage>({
    queryKey: ["entity-picker", query],
    queryFn: () => apiGet<EntityPage>("/api/entities", { q: query, limit: OPTIONS_LIMIT }),
    enabled: query !== "",
    staleTime: 30_000,
  });
  // Ответ на устаревший текст лежит под своим ключом и сюда не попадает; пока идёт пауза
  // после ввода, старые варианты тоже не показываются.
  const current = query !== "" && query === typed;
  const options: EntityListItem[] = current ? (results.data?.items ?? []).filter((o) => o.entity_id !== exclude) : [];
  const expanded = open && options.length > 0;
  const activeIndex = active < options.length ? active : -1;
  const optionId = (i: number) => `${ids.list}-${i}`;

  const selectedName =
    value === null ? "" : (selected.data?.name ?? (picked?.id === value ? picked.name : selected.isError ? value : ""));
  const shown = draft ?? selectedName;

  const choose = (option: EntityListItem) => {
    setPicked({ id: option.entity_id, name: option.name });
    setDraft(null);
    setOpen(false);
    setActive(-1);
    onChange(option.entity_id);
  };

  const clear = () => {
    setDraft("");
    setOpen(false);
    setActive(-1);
    onChange(null);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (options.length === 0) return;
      e.preventDefault();
      setOpen(true);
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => {
        const from = i < options.length ? i : -1;
        if (!expanded) return step > 0 ? 0 : options.length - 1;
        return (from + step + options.length) % options.length;
      });
    } else if (e.key === "Enter") {
      if (!expanded || activeIndex < 0) return;
      e.preventDefault();
      choose(options[activeIndex]!);
    } else if (e.key === "Escape") {
      if (!expanded) return;
      // Esc закрывает только список: страница графа не должна снимать выделение.
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      setActive(-1);
    }
  };

  const searching = open && typed !== "" && (!current || results.isFetching) && options.length === 0;
  const nothing = open && current && results.isSuccess && options.length === 0;

  return (
    <div className="relative">
      <label htmlFor={ids.input} className="mb-1 block text-[13px] font-medium text-muted">
        {label}
      </label>
      <div className="relative">
        <Search size={15} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-muted" />
        <input
          ref={inputRef}
          id={ids.input}
          type="text"
          role="combobox"
          aria-expanded={expanded}
          aria-controls={ids.list}
          aria-autocomplete="list"
          aria-activedescendant={expanded && activeIndex >= 0 ? optionId(activeIndex) : undefined}
          value={shown}
          placeholder="Начните вводить имя"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => {
            setDraft(e.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onFocus={() => {
            if (draft !== null) setOpen(true);
          }}
          onBlur={() => {
            setOpen(false);
            // Ушли, ничего не выбрав, — поле снова показывает выбранную сущность.
            if (value !== null) setDraft(null);
          }}
          onKeyDown={onKeyDown}
          className="w-full rounded-xl border border-line bg-card py-2 pr-9 pl-8 text-sm shadow-card outline-none placeholder:text-muted focus:border-primary"
        />
        {(value !== null || (draft ?? "") !== "") && (
          <button
            type="button"
            onClick={clear}
            aria-label="Очистить"
            title="Очистить"
            className="absolute top-1/2 right-1.5 grid size-7 -translate-y-1/2 place-items-center rounded-full text-muted transition-colors hover:bg-ink/[0.06] hover:text-ink focus-visible:outline-2 focus-visible:outline-primary"
          >
            <X size={14} />
          </button>
        )}
      </div>
      {/* Список всегда в DOM (на него указывает aria-controls), виден — только с вариантами. */}
      <ul
        id={ids.list}
        role="listbox"
        aria-label={label}
        hidden={!expanded}
        className="absolute inset-x-0 top-full z-50 mt-1 max-h-72 overflow-y-auto rounded-xl border border-line bg-raised p-1 shadow-raised"
      >
        {expanded &&
          options.map((o, i) => (
            <li
              key={o.entity_id}
              id={optionId(i)}
              role="option"
              aria-selected={i === activeIndex}
              // Фокус остаётся в поле: иначе blur закрыл бы список раньше клика.
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => choose(o)}
              className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm aria-selected:bg-primary/15"
            >
              <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: catalog.typeColor(o.type) }} aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{o.name}</span>
              <span className="shrink-0 text-xs text-muted">{catalog.typeLabel(o.type)}</span>
            </li>
          ))}
      </ul>
      <p role="status" className={searching || nothing ? "mt-1 px-1 text-xs text-muted" : "sr-only"}>
        {searching ? "Ищу…" : nothing ? "Ничего не нашлось." : ""}
      </p>
    </div>
  );
}
