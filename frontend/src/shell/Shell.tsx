import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ShellColumnContext, type ShellColumnState } from "./columnContext";
import { ContextColumn } from "./ContextColumn";
import { Rail } from "./Rail";
import { TopBar } from "./TopBar";

type Props = {
  pathname: string;
  /** Контекстная колонка раздела; null/undefined — её нет. */
  column?: ReactNode;
  /** Страница во всю площадь (граф): без полей и без прокрутки контента. */
  bleed?: boolean;
  children: ReactNode;
};

const SPRING = { type: "spring", stiffness: 380, damping: 38 } as const;

function isDesktop(): boolean {
  return typeof matchMedia === "function" && matchMedia("(min-width: 768px)").matches;
}

export function Shell({ pathname, column = null, bleed = false, children }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const hasColumn = column !== null && column !== undefined && column !== false;

  // Смена раздела закрывает мобильное меню.
  useEffect(() => setMenuOpen(false), [pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  const columnState = useMemo<ShellColumnState>(
    () => ({
      available: hasColumn,
      collapsed,
      setCollapsed,
      open: () => (isDesktop() ? setCollapsed(false) : setMenuOpen(true)),
    }),
    [hasColumn, collapsed],
  );

  return (
    <ShellColumnContext.Provider value={columnState}>
      <div className="flex h-dvh bg-bg text-ink">
        <Rail pathname={pathname} />
        <AnimatePresence initial={false}>
          {hasColumn && !collapsed && (
            <motion.div
              key="column"
              className="hidden shrink-0 overflow-hidden md:block"
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: 288, opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={SPRING}
            >
              <ContextColumn>{column}</ContextColumn>
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence>
          {hasColumn && menuOpen && (
            <motion.div
              key="menu-backdrop"
              className="fixed inset-0 z-30 bg-ink/20 md:hidden"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              onClick={() => setMenuOpen(false)}
            />
          )}
          {hasColumn && menuOpen && (
            <motion.aside
              key="menu"
              aria-label="Меню раздела"
              initial={{ x: "-100%" }}
              animate={{ x: 0 }}
              exit={{ x: "-100%" }}
              transition={SPRING}
              className="fixed inset-y-0 left-0 z-40 w-80 max-w-[88%] overflow-y-auto border-r border-line bg-card p-4 shadow-card md:hidden"
              onClick={(event) => {
                // Переход по ссылке закрывает меню; фильтры и прочие элементы — нет.
                if ((event.target as HTMLElement).closest("a")) setMenuOpen(false);
              }}
            >
              {column}
            </motion.aside>
          )}
        </AnimatePresence>
        <div className="flex min-w-0 flex-1 flex-col pb-[68px] md:pb-0">
          <TopBar onMenu={hasColumn ? () => setMenuOpen(true) : undefined} />
          <main
            className={
              bleed
                ? "min-h-0 flex-1 overflow-hidden"
                : "min-h-0 flex-1 overflow-y-auto px-4 pb-8 md:px-8"
            }
          >
            {/* Только вход: выходящий поддерев рендерил бы живой <Outlet/> уже нового маршрута. */}
            <motion.div
              key={pathname}
              className={bleed ? "h-full" : undefined}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16 }}
            >
              {children}
            </motion.div>
          </main>
        </div>
      </div>
    </ShellColumnContext.Provider>
  );
}
