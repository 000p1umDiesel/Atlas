import { X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, type ReactNode } from "react";

type Props = { open: boolean; onClose: () => void; title: string; children: ReactNode };

/**
 * Выезжающая справа панель. Намеренно не Radix Dialog: она немодальная,
 * не блокирует прокрутку и страницу за ней, не вставляет <style>.
 */
export function Sheet({ open, onClose, title, children }: Props) {
  const ref = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const onKey = (event: KeyboardEvent) => {
      // Esc, который уже обработан (закрыта подсказка внутри панели), панель не закрывает.
      if (event.key === "Escape" && !event.defaultPrevented) onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      const target = opener.current;
      opener.current = null;
      if (target?.isConnected) target.focus();
    };
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <motion.aside
          ref={ref}
          role="dialog"
          aria-modal="false"
          aria-label={title}
          tabIndex={-1}
          initial={{ x: "100%", opacity: 0.6 }}
          animate={{ x: 0, opacity: 1 }}
          exit={{ x: "100%", opacity: 0 }}
          transition={{ type: "spring", stiffness: 380, damping: 38 }}
          className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-line bg-card shadow-card outline-none"
        >
          <header className="flex items-center justify-between gap-3 px-6 pt-5 pb-3">
            <h2 className="text-lg font-semibold">{title}</h2>
            <button
              type="button"
              onClick={onClose}
              aria-label="Закрыть"
              className="grid size-9 place-items-center rounded-full text-muted transition hover:bg-bg hover:text-ink focus-visible:outline-2 focus-visible:outline-primary"
            >
              <X size={18} />
            </button>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6">{children}</div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
