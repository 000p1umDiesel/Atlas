import { Link } from "@tanstack/react-router";
import { LogOut, Menu, Search, User } from "lucide-react";
import { useEffect, useId, useRef } from "react";
import { paletteShortcut } from "./commands";
import { ThemeButton } from "./ThemeButton";

type Props = {
  /** Меню аккаунта (телефон) открыто. Состояние у оболочки: она закрывает меню при смене пути. */
  accountOpen: boolean;
  onAccountOpenChange(open: boolean): void;
  onMenu?: () => void;
  /** Открыть палитру Ctrl+K. */
  onSearch: () => void;
  username?: string;
  onLogout: () => void;
  logoutBusy?: boolean;
};

const ICON_BUTTON =
  "grid size-10 shrink-0 place-items-center rounded-full text-muted hover:bg-card hover:text-ink focus-visible:outline-2 focus-visible:outline-primary md:hidden";
const MENU_ITEM =
  "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm text-ink hover:bg-bg focus-visible:outline-2 focus-visible:outline-primary disabled:opacity-50";

/** Меню аккаунта для телефона: на широком экране «Аккаунт» и «Выйти» стоят в рейке. */
function AccountMenu({
  open,
  setOpen,
  username,
  onLogout,
  logoutBusy,
}: Pick<Props, "username" | "onLogout" | "logoutBusy"> & { open: boolean; setOpen(open: boolean): void }) {
  const menuId = useId();
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      // Esc, уже обработанный внутренним элементом, меню не закрывает.
      if (event.key !== "Escape" || event.defaultPrevented) return;
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
  }, [open, setOpen]);

  return (
    <div ref={root} className="relative md:hidden">
      <button
        ref={toggle}
        type="button"
        aria-label="Меню аккаунта"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen(!open)}
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

export function TopBar({ accountOpen, onAccountOpenChange, onMenu, onSearch, username, onLogout, logoutBusy }: Props) {
  const shortcut = paletteShortcut();
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
        onClick={onSearch}
        aria-label="Поиск"
        aria-keyshortcuts={shortcut === "⌘K" ? "Meta+K" : "Control+K"}
        title={`Поиск (${shortcut})`}
        className="flex min-w-0 max-w-xs shrink-0 items-center gap-2 rounded-full border border-line bg-card px-3 py-2 text-sm text-muted shadow-card transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-primary sm:w-full sm:shrink sm:px-4"
      >
        <Search size={16} className="shrink-0" aria-hidden="true" />
        {/* На телефоне — только значок: подпись не помещается. */}
        <span className="hidden flex-1 truncate text-left sm:inline">Поиск</span>
        <kbd className="hidden shrink-0 rounded-md border border-line bg-bg px-1.5 py-0.5 font-sans text-xs font-medium text-muted sm:inline">
          {shortcut}
        </kbd>
      </button>
      {/* На телефоне в нижней панели нет места: тема и аккаунт здесь; на широком экране — в рейке. */}
      <ThemeButton className={ICON_BUTTON} />
      <AccountMenu open={accountOpen} setOpen={onAccountOpenChange} username={username} onLogout={onLogout} logoutBusy={logoutBusy} />
    </header>
  );
}
