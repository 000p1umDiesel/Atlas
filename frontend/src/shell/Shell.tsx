import { AnimatePresence, motion } from "motion/react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMe } from "../api/auth";
import { CommandPalette } from "./CommandPalette";
import { useMediaQuery } from "../lib/useMediaQuery";
import { ShellColumnContext, type ShellColumnState } from "./columnContext";
import { ContextColumn } from "./ContextColumn";
import { MainScrollContext } from "./mainScroll";
import { ShellOverlayContext } from "./overlay";
import { PanelHost } from "./PanelHost";
import { Rail } from "./Rail";
import { TopBar } from "./TopBar";
import { useLogout } from "./useLogout";

type Props = {
  pathname: string;
  /** Контекстная колонка раздела; null/undefined — её нет. */
  column?: ReactNode;
  /** Страница во всю площадь (граф): без полей и без прокрутки контента. */
  bleed?: boolean;
  children: ReactNode;
};

const SPRING = { type: "spring", stiffness: 380, damping: 38 } as const;

/** Раздел — первый сегмент пути: `/chat/c1` → `chat`. */
function section(pathname: string): string {
  return pathname.split("/")[1] ?? "";
}

function isGraph(pathname: string): boolean {
  return pathname === "/graph" || pathname.startsWith("/graph/");
}

export function Shell({ pathname, column = null, bleed = false, children }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const hasColumn = column !== null && column !== undefined && column !== false;
  // Проверка сессии на любой странице оболочки: без входа /api/auth/me ответит 401 — на вход.
  const username = useMe().data?.username;
  const { logout, busy: logoutBusy } = useLogout();

  // Смена пути (в том числе «назад») закрывает мобильные меню раздела и аккаунта.
  useEffect(() => {
    setMenuOpen(false);
    setAccountOpen(false);
  }, [pathname]);

  // Окно стало широким: оба меню — телефонные (md:hidden), открытыми их не оставляем.
  const desktop = useMediaQuery("(min-width: 768px)", false);
  useEffect(() => {
    if (!desktop) return;
    setMenuOpen(false);
    setAccountOpen(false);
  }, [desktop]);

  // Новая страница открывается с начала: прокручивается <main>, а не окно. Только по пути —
  // правая панель (`?panel=`) и поиск (`?q=`) прокрутку не сбрасывают. Эффект разметки
  // успевает до эффектов страницы (прокрутка читалки к `?chunk` идёт после него).
  const mainRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (mainRef.current) mainRef.current.scrollTop = 0;
  }, [pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      // Esc уже обработал внутренний элемент (список подсказок, диалог) — меню остаётся.
      if (event.key === "Escape" && !event.defaultPrevented) setMenuOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  const columnState = useMemo<ShellColumnState>(
    () => ({
      available: hasColumn,
      collapsed,
      setCollapsed,
      open: () => (desktop ? setCollapsed(false) : setMenuOpen(true)),
    }),
    [hasColumn, collapsed, desktop],
  );
  const overlayOpen = (hasColumn && menuOpen) || accountOpen;

  return (
    <ShellColumnContext.Provider value={columnState}>
      <ShellOverlayContext.Provider value={overlayOpen}>
        <MainScrollContext.Provider value={mainRef}>
          <div className="flex h-dvh bg-bg text-ink">
            <Rail pathname={pathname} username={username} onLogout={logout} logoutBusy={logoutBusy} />
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
                  data-menu-backdrop=""
                  className="fixed inset-0 z-30 bg-scrim md:hidden"
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
              <TopBar
                accountOpen={accountOpen}
                onAccountOpenChange={setAccountOpen}
                onMenu={hasColumn ? () => setMenuOpen(true) : undefined}
                onSearch={() => setPaletteOpen(true)}
                username={username}
                onLogout={logout}
                logoutBusy={logoutBusy}
              />
              <main
                ref={mainRef}
                className={
                  bleed
                    ? "min-h-0 flex-1 overflow-hidden"
                    : "min-h-0 flex-1 overflow-y-auto px-4 pb-8 md:px-8"
                }
              >
                {/* Только вход: выходящий поддерев рендерил бы живой <Outlet/> уже нового маршрута.
                    Ключ — раздел: переход между диалогами не гасит и не перемонтирует страницу. */}
                <motion.div
                  key={section(pathname)}
                  className={bleed ? "h-full" : undefined}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.16 }}
                >
                  {children}
                </motion.div>
              </main>
            </div>
            {/* У графа своя карточка сущности: правая панель там не нужна. */}
            {!isGraph(pathname) && <PanelHost />}
            {/* Ctrl+K — на любой странице оболочки (на /login оболочки нет). */}
            <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
          </div>
        </MainScrollContext.Provider>
      </ShellOverlayContext.Provider>
    </ShellColumnContext.Provider>
  );
}
