import { Link } from "@tanstack/react-router";
import { LogOut, Menu, Search, User } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { ThemeButton } from "./ThemeButton";

type Props = {
  onMenu?: () => void;
  username?: string;
  onLogout: () => void;
  logoutBusy?: boolean;
};

const ICON_BUTTON =
  "grid size-10 shrink-0 place-items-center rounded-full text-muted hover:bg-card hover:text-ink focus-visible:outline-2 focus-visible:outline-primary md:hidden";
const MENU_ITEM =
  "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm text-ink hover:bg-bg focus-visible:outline-2 focus-visible:outline-primary disabled:opacity-50";

/** Меню аккаунта для телефона: на широком экране «Аккаунт» и «Выйти» стоят в рейке. */
function AccountMenu({ username, onLogout, logoutBusy }: Omit<Props, "onMenu">) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      toggle.current?.focus();
    };
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return (
    <div ref={root} className="relative md:hidden">
      <button
        ref={toggle}
        type="button"
        aria-label="Меню аккаунта"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
        className={ICON_BUTTON}
      >
        <User size={20} strokeWidth={1.8} />
      </button>
      {open && (
        <div
          id={menuId}
          className="absolute top-12 right-0 z-40 w-56 rounded-[14px] border border-line bg-card p-1.5 shadow-card"
        >
          {username && <p className="truncate px-3 pt-2 pb-1 text-xs text-muted">{username}</p>}
          <Link to="/account" onClick={() => setOpen(false)} className={MENU_ITEM}>
            <User size={16} strokeWidth={1.8} aria-hidden="true" />
            Аккаунт
          </Link>
          <button type="button" onClick={onLogout} disabled={logoutBusy} className={MENU_ITEM}>
            <LogOut size={16} strokeWidth={1.8} aria-hidden="true" />
            Выйти
          </button>
        </div>
      )}
    </div>
  );
}

export function TopBar({ onMenu, username, onLogout, logoutBusy }: Props) {
  return (
    <header className="flex h-16 shrink-0 items-center gap-2 px-4 md:gap-3 md:px-8">
      {onMenu && (
        <button type="button" onClick={onMenu} aria-label="Меню раздела" className={ICON_BUTTON}>
          <Menu size={20} />
        </button>
      )}
      <span className="shrink-0 font-serif text-xl font-semibold tracking-tight text-primary-ink">
        mnogobase
      </span>
      <div className="flex-1" />
      <button
        type="button"
        disabled
        aria-label="Поиск (скоро)"
        className="flex min-w-0 max-w-xs shrink-0 items-center sm:w-full sm:shrink gap-2 rounded-full border border-line bg-card px-4 py-2 text-sm text-muted shadow-card disabled:cursor-not-allowed"
      >
        <Search size={16} className="shrink-0" />
        {/* На телефоне — только значок: подпись не помещается. */}
        <span className="hidden truncate sm:inline">Ctrl+K — скоро</span>
      </button>
      {/* На телефоне в нижней панели нет места: тема и аккаунт здесь; на широком экране — в рейке. */}
      <ThemeButton className={ICON_BUTTON} />
      <AccountMenu username={username} onLogout={onLogout} logoutBusy={logoutBusy} />
    </header>
  );
}
