import { Menu, Search } from "lucide-react";
import { ThemeButton } from "./ThemeButton";

type Props = { onMenu?: () => void };

export function TopBar({ onMenu }: Props) {
  return (
    <header className="flex h-16 shrink-0 items-center gap-3 px-4 md:px-8">
      {onMenu && (
        <button
          type="button"
          onClick={onMenu}
          aria-label="Меню раздела"
          className="grid size-10 place-items-center rounded-full text-muted hover:bg-card hover:text-ink md:hidden"
        >
          <Menu size={20} />
        </button>
      )}
      <span className="font-serif text-xl font-semibold tracking-tight text-primary-ink">
        mnogobase
      </span>
      <div className="flex-1" />
      <button
        type="button"
        disabled
        aria-label="Поиск (скоро)"
        className="flex w-full max-w-xs items-center gap-2 rounded-full border border-line bg-card px-4 py-2 text-sm text-muted shadow-card disabled:cursor-not-allowed"
      >
        <Search size={16} />
        <span className="truncate">Ctrl+K — скоро</span>
      </button>
      {/* На телефоне в нижней панели нет места: тема здесь; на широком экране — в рейке. */}
      <ThemeButton className="grid size-10 shrink-0 place-items-center rounded-full text-muted hover:bg-card hover:text-ink focus-visible:outline-2 focus-visible:outline-primary md:hidden" />
    </header>
  );
}
