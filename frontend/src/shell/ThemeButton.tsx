import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { useTheme, type ThemeChoice } from "../lib/theme";

const THEME_CYCLE: Record<ThemeChoice, { next: ThemeChoice; label: string; icon: LucideIcon }> = {
  system: { next: "light", label: "Тема: как в системе", icon: Monitor },
  light: { next: "dark", label: "Тема: светлая", icon: Sun },
  dark: { next: "system", label: "Тема: тёмная", icon: Moon },
};

/** Кнопка темы: по кругу «как в системе» → светлая → тёмная. Стоит в рейке (широкий экран)
 * и в верхней панели (телефон); выбор общий (`useTheme`). */
export function ThemeButton({ className }: { className: string }) {
  const [theme, setTheme] = useTheme();
  const cycle = THEME_CYCLE[theme];
  const Icon = cycle.icon;
  return (
    <button
      type="button"
      className={className}
      aria-label={cycle.label}
      title={cycle.label}
      onClick={() => setTheme(cycle.next)}
    >
      <Icon size={20} strokeWidth={1.8} />
    </button>
  );
}
