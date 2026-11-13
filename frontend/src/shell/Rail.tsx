import { Link } from "@tanstack/react-router";
import {
  Circle,
  FileText,
  Home,
  MessageCircle,
  Monitor,
  Moon,
  Network,
  Notebook,
  Activity,
  Sun,
  User,
  type LucideIcon,
} from "lucide-react";
import { motion } from "motion/react";
import { useTheme, type ThemeChoice } from "../lib/theme";

export const NAV_ITEMS: { to: string; label: string; icon: LucideIcon }[] = [
  { to: "/", label: "Главная", icon: Home },
  { to: "/chat", label: "Чат", icon: MessageCircle },
  { to: "/documents", label: "Документы", icon: FileText },
  { to: "/entities", label: "Сущности", icon: Circle },
  { to: "/graph", label: "Граф", icon: Network },
  { to: "/wiki", label: "Wiki", icon: Notebook },
  { to: "/status", label: "Статус", icon: Activity },
];

const THEME_CYCLE: Record<ThemeChoice, { next: ThemeChoice; label: string; icon: LucideIcon }> = {
  system: { next: "light", label: "Тема: как в системе", icon: Monitor },
  light: { next: "dark", label: "Тема: светлая", icon: Sun },
  dark: { next: "system", label: "Тема: тёмная", icon: Moon },
};

const BUTTON =
  "relative grid size-11 place-items-center rounded-[14px] text-muted transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-primary";

/** Иконная рейка слева; на узких экранах — нижняя панель. */
export function Rail({ pathname }: { pathname: string }) {
  const [theme, setTheme] = useTheme();
  const cycle = THEME_CYCLE[theme];
  const ThemeIcon = cycle.icon;
  return (
    <nav
      aria-label="Разделы"
      className="fixed inset-x-0 bottom-0 z-30 flex items-center justify-around border-t border-line bg-card/95 px-2 py-2 backdrop-blur md:static md:inset-auto md:w-[72px] md:shrink-0 md:flex-col md:justify-start md:gap-1 md:border-t-0 md:border-r md:bg-card md:px-0 md:py-4"
    >
      <div className="mb-3 hidden size-10 place-items-center rounded-[14px] bg-primary font-serif text-lg font-semibold text-white md:grid" aria-hidden="true">
        m
      </div>
      {NAV_ITEMS.map(({ to, label, icon: Icon }) => {
        const active = to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(to + "/");
        return (
          <Link
            key={to}
            to={to}
            aria-label={label}
            title={label}
            className={`${BUTTON} ${active ? "text-primary-ink" : ""}`}
          >
            {active && (
              <motion.span
                layoutId="rail-active"
                className="absolute inset-0 rounded-[14px] bg-primary/15"
                transition={{ type: "spring", stiffness: 450, damping: 36 }}
              />
            )}
            <Icon size={21} strokeWidth={active ? 2.2 : 1.8} className="relative" />
          </Link>
        );
      })}
      <div className="hidden flex-1 md:block" />
      <button
        type="button"
        className={`${BUTTON} hidden md:grid`}
        aria-label={cycle.label}
        title={cycle.label}
        onClick={() => setTheme(cycle.next)}
      >
        <ThemeIcon size={20} strokeWidth={1.8} />
      </button>
      <button type="button" className={`${BUTTON} hidden md:grid`} aria-label="Аккаунт" title="Аккаунт (скоро)">
        <User size={20} strokeWidth={1.8} />
      </button>
    </nav>
  );
}
