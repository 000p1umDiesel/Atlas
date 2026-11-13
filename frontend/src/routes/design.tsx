import { useTypeCatalog } from "../api/queries";
import { useTheme, type ThemeChoice } from "../lib/theme";
import { typeColorAt } from "../lib/typePalette";

const THEMES: [ThemeChoice, string][] = [
  ["system", "Как в системе"],
  ["light", "Светлая"],
  ["dark", "Тёмная"],
];

/** Стилевая плитка: по ней владелец оценивает палитру, шрифты и компоненты. */
export function DesignPage() {
  const [theme, setTheme] = useTheme();
  const catalog = useTypeCatalog();
  // Пример чипов — первые два типа из списка (без данных — ничего).
  const chips = catalog.order.slice(0, 2);
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
        <p className="text-sm text-muted">
          Цвет даёт различимый генератор по порядку типов в config.yaml, своя пара для каждой темы. Тип вне
          списка — нейтральный.
        </p>
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {catalog.order.map((type, i) => {
            const c = typeColorAt(i);
            return (
              <li key={type} className="flex items-center gap-3 rounded-card bg-card p-3 shadow-card">
                <span className="size-10 shrink-0 rounded-xl" style={{ backgroundColor: catalog.typeColor(type) }} />
                <span className="min-w-0 leading-tight">
                  <span className="block truncate font-medium">{catalog.typeLabel(type)}</span>
                  <span className="block text-sm text-muted tabular-nums">
                    {c.light} / {c.dark}
                  </span>
                </span>
              </li>
            );
          })}
          <li className="flex items-center gap-3 rounded-card bg-card p-3 shadow-card">
            <span className="size-10 shrink-0 rounded-xl bg-t0" />
            <span className="leading-tight">
              <span className="block font-medium">Другой тип</span>
              <span className="block text-sm text-muted tabular-nums">--t0 #BDB6A8 / #7E7B70</span>
            </span>
          </li>
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
          {chips.map((type) => (
            <span key={type} className="inline-flex items-center gap-1.5 rounded-full border border-line bg-bg px-3 py-1 text-sm">
              <span className="size-2.5 rounded-full" style={{ backgroundColor: catalog.typeColor(type) }} aria-hidden="true" />
              {catalog.typeLabel(type)}
            </span>
          ))}
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
