import { useTheme, type ThemeChoice } from "../lib/theme";

const TYPES = [
  ["t0", "нейтральный", "#C9C3B6", "bg-t0"],
  ["t1", "глина", "#D8A48F", "bg-t1"],
  ["t2", "небо", "#9FBFD3", "bg-t2"],
  ["t3", "лаванда", "#BDB0D8", "bg-t3"],
  ["t4", "масло", "#E5CF85", "bg-t4"],
  ["t5", "роза", "#E2B2B4", "bg-t5"],
  ["t6", "мята", "#A9D3BC", "bg-t6"],
  ["t7", "персик", "#EBC09A", "bg-t7"],
  ["t8", "олива", "#B7C08A", "bg-t8"],
] as const;

const THEMES: [ThemeChoice, string][] = [
  ["system", "Как в системе"],
  ["light", "Светлая"],
  ["dark", "Тёмная"],
];

/** Стилевая плитка: по ней владелец оценивает палитру, шрифты и компоненты. */
export function DesignPage() {
  const [theme, setTheme] = useTheme();
  return (
    <main className="mx-auto max-w-4xl space-y-10 px-6 py-10">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold">Стилевая плитка</h1>
          <p className="mt-1 text-muted">Палитра, шрифты и базовые элементы mnogobase.</p>
        </div>
        <div role="group" aria-label="Тема" className="flex gap-1 rounded-full bg-card p-1 shadow-card">
          {THEMES.map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={theme === value}
              onClick={() => setTheme(value)}
              className={`rounded-full px-3 py-1.5 text-sm transition-colors ${
                theme === value ? "bg-primary-deep text-on-primary" : "text-muted hover:text-ink"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </header>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Поверхности и текст</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-card border border-line bg-bg p-5">
            <p className="text-sm text-muted">Фон страницы</p>
            <p className="mt-2 font-serif text-lg">Длинный текст набран Literata: спокойно, как на бумаге.</p>
          </div>
          <div className="rounded-card bg-card p-5 shadow-card">
            <p className="text-sm text-muted">Карточка</p>
            <p className="mt-2">Интерфейс набран Golos Text. Ответ со ссылками на источники [1].</p>
            <p className="mt-2 text-muted">Приглушённый текст для подписей и подсказок.</p>
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Цвета типов сущностей</h2>
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {TYPES.map(([name, label, hex, cls]) => (
            <li key={name} className="flex items-center gap-3 rounded-card bg-card p-3 shadow-card">
              <span className={`size-10 shrink-0 rounded-xl ${cls}`} />
              <span className="leading-tight">
                <span className="block font-medium">{label}</span>
                <span className="block text-sm text-muted">
                  --{name} {hex}
                </span>
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Элементы</h2>
        <div className="flex flex-wrap items-center gap-3 rounded-card bg-card p-5 shadow-card">
          <button type="button" className="rounded-xl bg-primary-deep px-4 py-2 text-on-primary shadow-card hover:opacity-90">
            Главная кнопка
          </button>
          <button type="button" className="rounded-xl px-4 py-2 text-primary-ink hover:bg-primary/15">
            Тихая кнопка
          </button>
          <span className="rounded-full bg-t6 px-3 py-1 text-sm text-ink">мята</span>
          <span className="rounded-full bg-t2 px-3 py-1 text-sm text-ink">небо</span>
          <input
            aria-label="Пример поля"
            placeholder="Задайте вопрос базе знаний"
            className="min-w-60 flex-1 rounded-xl border border-line bg-bg px-3 py-2 outline-none placeholder:text-muted focus:border-primary"
          />
        </div>
      </section>
    </main>
  );
}
