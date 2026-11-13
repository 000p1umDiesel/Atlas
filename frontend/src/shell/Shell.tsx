import { AnimatePresence, motion } from "motion/react";
import { useState, type ReactNode } from "react";
import { ContextColumn } from "./ContextColumn";
import { Rail } from "./Rail";
import { TopBar } from "./TopBar";

type Props = {
  pathname: string;
  /** Контекстная колонка раздела; null/undefined — свёрнута. */
  column?: ReactNode;
  children: ReactNode;
};

export function Shell({ pathname, column = null, children }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <div className="flex h-dvh bg-bg text-ink">
      <Rail pathname={pathname} />
      {column && <ContextColumn>{column}</ContextColumn>}
      <AnimatePresence>
        {column && menuOpen && (
          <motion.aside
            key="menu"
            aria-label="Меню раздела"
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ type: "spring", stiffness: 380, damping: 38 }}
            className="fixed inset-y-0 left-0 z-40 w-72 max-w-[85%] overflow-y-auto border-r border-line bg-card p-4 shadow-card md:hidden"
            onClick={() => setMenuOpen(false)}
          >
            {column}
          </motion.aside>
        )}
      </AnimatePresence>
      <div className="flex min-w-0 flex-1 flex-col pb-[68px] md:pb-0">
        <TopBar onMenu={column ? () => setMenuOpen(true) : undefined} />
        <main className="min-h-0 flex-1 overflow-y-auto px-4 pb-8 md:px-8">
          {/* Только вход: выходящий поддерев рендерил бы живой <Outlet/> уже нового маршрута. */}
          <motion.div
            key={pathname}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.16 }}
          >
            {children}
          </motion.div>
        </main>
      </div>
    </div>
  );
}
