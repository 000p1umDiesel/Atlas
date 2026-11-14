import { useRouter } from "@tanstack/react-router";
import { BookOpen, CornerDownLeft, FileText, MessageSquare, Search, Zap } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import { ApiError } from "../api/client";
import { useTypeCatalog } from "../api/queries";
import { useQuickSearch } from "../api/search";
import { useTheme, type ThemeChoice } from "../lib/theme";
import { StatusBadge } from "../ui/StatusBadge";
import { filterActions, isPaletteHotkey, nextTheme } from "./commands";

/** Пауза после ввода перед запросом, мс. */
const DEBOUNCE_MS = 150;
/** Предел сервера для `q`. */
const MAX_QUERY = 200;

const THEME_NAMES: Record<ThemeChoice, string> = { system: "как в системе", light: "светлая", dark: "тёмная" };

type Props = { open: boolean; onOpenChange(open: boolean): void };

/** `server` — пункт из ответа сервера (может устареть), иначе действие. */
type Item = { key: string; server: boolean; run(): void; content: ReactNode };
type Group = { id: string; label: string; items: Item[] };

/**
 * Палитра Ctrl+K: быстрый поиск и действия. Свой модальный слой (без Radix и cmdk: CSP и одна
 * точка фокуса). Ctrl+K / ⌘K открывает и закрывает её отовсюду, включая поля ввода; Esc
 * закрывает и дальше не идёт — страница графа не снимает выделение.
 */
export function CommandPalette({ open, onOpenChange }: Props) {
  const reduced = useReducedMotion() ?? false;
  const inputRef = useRef<HTMLInputElement>(null);
  const openRef = useRef(open);
  const changeRef = useRef(onOpenChange);
  // Номер открытия — ключ содержимого: Ctrl+K во время анимации закрытия даёт новое пустое
  // поле, а не возвращает прежний текст и выделение. Счёт — при смене пропса во время рендера.
  const [session, setSession] = useState(0);
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setSession((n) => n + 1);
  }

  useEffect(() => {
    openRef.current = open;
    changeRef.current = onOpenChange;
  });

  // Перехват на window в фазе погружения: раньше любых обработчиков на document и в React.
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (isPaletteHotkey(event)) {
        event.preventDefault();
        event.stopPropagation();
        changeRef.current(!openRef.current);
      } else if (openRef.current && event.key === "Escape" && !event.isComposing) {
        event.preventDefault();
        event.stopPropagation();
        changeRef.current(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  // Фокус: при открытии — в поле (откуда пришли — запоминаем), при закрытии — обратно.
  // Эффект родителя идёт после эффектов детей, поэтому activeElement здесь ещё прежний.
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    inputRef.current?.focus();
    return () => {
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, [open]);

  const fade = reduced ? { duration: 0 } : { duration: 0.14 };
  return (
    <AnimatePresence>
      {open && (
        <div key={`palette-${session}`} className="fixed inset-0 z-50 flex items-start justify-center px-4 pt-3 sm:pt-[12vh]">
          <motion.div
            data-palette-backdrop=""
            aria-hidden="true"
            className="absolute inset-0 bg-scrim"
            initial={reduced ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={reduced ? undefined : { opacity: 0 }}
            transition={fade}
            onClick={() => onOpenChange(false)}
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Поиск"
            className="relative flex max-h-[min(560px,calc(100dvh-24px))] w-full max-w-xl flex-col overflow-hidden rounded-[14px] border border-line bg-card shadow-card sm:max-h-[70vh]"
            initial={reduced ? false : { opacity: 0, y: -8, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? undefined : { opacity: 0, y: -8, scale: 0.98 }}
            transition={fade}
          >
            <PaletteBody inputRef={inputRef} onClose={() => onOpenChange(false)} />
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}

/** Содержимое: монтируется при каждом открытии — поле и выделение начинаются заново. */
function PaletteBody({ inputRef, onClose }: { inputRef: RefObject<HTMLInputElement | null>; onClose(): void }) {
  // Своё поле — для Tab; общий ref родителя получает последнее смонтированное поле и не
  // обнуляется уходящим (во время анимации закрытия старое и новое содержимое живут вместе).
  const ownInput = useRef<HTMLInputElement | null>(null);
  const router = useRouter();
  const catalog = useTypeCatalog();
  const [theme, setTheme] = useTheme();
  const ids = { list: useId(), status: useId() };
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  // Выделение двигали стрелками или мышью после последнего ввода.
  const [moved, setMoved] = useState(false);

  const typed = text.trim();
  useEffect(() => {
    const t = setTimeout(() => setQuery(typed), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [typed]);

  // Пустое поле — только действия: ни запроса, ни прежнего ответа.
  const search = useQuickSearch(typed === "" ? "" : query);
  const data = typed !== "" && query !== "" ? search.data : undefined;

  const go = (href: string) => {
    onClose();
    void router.navigate({ href });
  };
  const path = (section: string, id: string) => `/${section}/${encodeURIComponent(id)}`;

  const groups: Group[] = [];
  if (data) {
    groups.push(
      {
        id: "entities",
        label: "Сущности",
        items: data.entities.map((e) => ({
          key: `e:${e.entity_id}`,
          server: true,
          run: () => go(path("entities", e.entity_id)),
          content: (
            <>
              <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: catalog.typeColor(e.type) }} aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{e.name}</span>
              <span className="shrink-0 text-xs text-muted">{catalog.typeLabel(e.type)}</span>
            </>
          ),
        })),
      },
      {
        id: "wiki",
        label: "Wiki",
        items: data.wiki.map((w) => ({
          key: `w:${w.page_id}`,
          server: true,
          run: () => go(path("wiki", w.slug)),
          content: (
            <>
              <BookOpen size={15} className="shrink-0 text-muted" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{w.title}</span>
            </>
          ),
        })),
      },
      {
        id: "documents",
        label: "Документы",
        items: data.documents.map((d) => ({
          key: `d:${d.id}`,
          server: true,
          run: () => go(path("documents", d.id)),
          content: (
            <>
              <FileText size={15} className="shrink-0 text-muted" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{d.name}</span>
              <StatusBadge value={d.status} />
            </>
          ),
        })),
      },
      {
        id: "conversations",
        label: "Диалоги",
        items: data.conversations.map((c) => ({
          key: `c:${c.id}`,
          server: true,
          run: () => go(path("chat", c.id)),
          content: (
            <>
              <MessageSquare size={15} className="shrink-0 text-muted" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{c.title || "Без названия"}</span>
            </>
          ),
        })),
      },
    );
  }
  groups.push({
    id: "actions",
    label: "Действия",
    items: filterActions(typed).map((a) => ({
      key: `a:${a.id}`,
      server: false,
      run:
        a.run === "theme"
          ? () => {
              setTheme(nextTheme(theme));
              onClose();
            }
          : () => go(a.to ?? "/"),
      content: (
        <>
          <Zap size={15} className="shrink-0 text-muted" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate">{a.label}</span>
          {a.run === "theme" && <span className="shrink-0 text-xs text-muted">сейчас: {THEME_NAMES[theme]}</span>}
        </>
      ),
    })),
  });
  const shown = groups.filter((g) => g.items.length > 0);
  const items = shown.flatMap((g) => g.items);
  // Ответ сервера устарел: текст ещё в паузе перед запросом или виден прежний ответ, пока
  // идёт новый. Тогда пункты сервера приглушены и по умолчанию выделено первое действие.
  const stale = data !== undefined && (query !== typed || search.isPlaceholderData);
  const firstAction = items.findIndex((item) => !item.server);
  const activeIndex =
    items.length === 0 ? -1 : stale && !moved ? firstAction : Math.min(active, items.length - 1);
  const optionId = (i: number) => `${ids.list}-${i}`;

  // Выделенный пункт виден и при прокрутке списка стрелками.
  useEffect(() => {
    if (activeIndex < 0) return;
    document.getElementById(optionId(activeIndex))?.scrollIntoView?.({ block: "nearest" });
  });

  const error = typed !== "" && search.isError ? (search.error instanceof ApiError ? search.error.detail : "Не удалось выполнить поиск") : null;
  const pending = typed !== "" && !error && (stale || (!data && (query !== typed || search.isFetching)));
  const nothing = typed !== "" && !pending && !error && items.length === 0;

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length === 0) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(activeIndex < 0 ? (step > 0 ? 0 : items.length - 1) : (activeIndex + step + items.length) % items.length);
      setMoved(true);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = activeIndex >= 0 ? items[activeIndex] : undefined;
      // Устаревший пункт сервера может не подходить к введённому тексту: ждём свежий ответ.
      if (item && !(stale && item.server)) item.run();
    }
  };

  let index = 0;
  return (
    <div
      className="flex min-h-0 flex-col"
      onKeyDown={(e) => {
        // Tab не уходит из диалога: в нём единственное поле фокуса.
        if (e.key === "Tab") {
          e.preventDefault();
          ownInput.current?.focus();
        }
      }}
    >
      <div className="flex items-center gap-2.5 border-b border-line px-4">
        <Search size={17} className="shrink-0 text-muted" aria-hidden="true" />
        <input
          ref={(el) => {
            ownInput.current = el;
            if (el) inputRef.current = el;
          }}
          type="text"
          role="combobox"
          aria-label="Поиск"
          aria-expanded={items.length > 0}
          aria-controls={ids.list}
          aria-autocomplete="list"
          aria-describedby={ids.status}
          aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
          value={text}
          maxLength={MAX_QUERY}
          placeholder="Сущность, страница, документ, диалог или действие"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => {
            setText(e.target.value);
            setActive(0);
            setMoved(false);
          }}
          onKeyDown={onKeyDown}
          className="min-w-0 flex-1 bg-transparent py-3.5 text-[15px] outline-none placeholder:text-muted"
        />
        <kbd className="hidden shrink-0 rounded-md border border-line bg-bg px-1.5 py-0.5 font-sans text-xs font-medium text-muted sm:inline">
          Esc
        </kbd>
      </div>
      <div id={ids.list} role="listbox" aria-label="Результаты" aria-busy={stale} className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {shown.map((group) => {
          const headingId = `${ids.list}-${group.id}`;
          return (
            <div
              key={group.id}
              role="group"
              aria-labelledby={headingId}
              className={stale && group.id !== "actions" ? "pb-1 opacity-55 transition-opacity" : "pb-1 transition-opacity"}
            >
              <div id={headingId} role="presentation" className="px-2.5 pt-2 pb-1 text-xs font-medium text-muted">
                {group.label}
              </div>
              {group.items.map((item) => {
                const i = index++;
                const selected = i === activeIndex;
                return (
                  <div
                    key={item.key}
                    id={optionId(i)}
                    role="option"
                    aria-selected={selected}
                    // Фокус остаётся в поле.
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseMove={() => {
                      if (selected) return;
                      setActive(i);
                      setMoved(true);
                    }}
                    onClick={() => item.run()}
                    className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm aria-selected:bg-primary/15"
                  >
                    {item.content}
                    {selected && <CornerDownLeft size={14} className="hidden shrink-0 text-muted sm:block" aria-hidden="true" />}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
      <p
        id={ids.status}
        role={error ? "alert" : "status"}
        className={error ? "border-t border-line px-4 py-2.5 text-sm text-danger" : pending || nothing ? "border-t border-line px-4 py-2.5 text-sm text-muted" : "sr-only"}
      >
        {error ?? (pending ? "Ищу…" : nothing ? "Ничего не нашлось." : "")}
      </p>
    </div>
  );
}
