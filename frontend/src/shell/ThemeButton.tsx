import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { useTheme, type ThemeChoice } from "../lib/theme";
import { nextTheme } from "./commands";

const THEME_VIEW: Record<ThemeChoice, { label: string; icon: LucideIcon }> = {
  system: { label: "Тема: как в системе", icon: Monitor },
  light: { label: "Тема: светлая", icon: Sun },
  dark: { label: "Тема: тёмная", icon: Moon },
};

/** Кнопка темы: по кругу «как в системе» → светлая → тёмная. Стоит в рейке (широкий экран)
 * и в верхней панели (телефон); выбор общий (`useTheme`). */
export function ThemeButton({ className }: { className: string }) {
  const [theme, setTheme] = useTheme();
  const cycle = THEME_VIEW[theme];
  const Icon = cycle.icon;
  return (
    <button
      type="button"
      className={className}
      aria-label={cycle.label}
      title={cycle.label}
      onClick={() => setTheme(nextTheme(theme))}
    >
      <Icon size={20} strokeWidth={1.8} />
    </button>
  );
}
