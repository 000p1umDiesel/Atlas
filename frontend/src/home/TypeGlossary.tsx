import { BookOpen, ChevronDown } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { TypeCatalog } from "../lib/typeCatalog";

/** Кнопка под полосами типов: раскрывает справочник ниже. */
export function GlossaryToggle({ open, controls, onToggle }: { open: boolean; controls: string; onToggle(): void }) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-sm text-primary-ink transition-colors hover:bg-primary/10 focus-visible:outline-2 focus-visible:outline-primary"
    >
      <BookOpen size={15} strokeWidth={1.8} aria-hidden="true" />
      Что это за типы?
      <ChevronDown size={15} aria-hidden="true" className={`transition-transform ${open ? "rotate-180" : ""}`} />
    </button>
  );
}

/** Отступ сетки главной (`gap-y-12`): закрытый справочник не должен оставлять пустой зазор. */
const GRID_GAP = "-3rem";

/**
 * Справочник типов сущностей: цветная точка, русское название, определение. Читается как
 * словарь — колонки текут сверху вниз (1 → 2 → 3 по ширине), порядок — как в config.yaml.
 * Раскрывается плавно по высоте; при prefers-reduced-motion — сразу (MotionConfig).
 */
export function TypeGlossary({ id, open, catalog, className = "" }: { id: string; open: boolean; catalog: TypeCatalog; className?: string }) {
  const entries = catalog.order.map((t) => catalog.typeInfo(t)).filter((t) => t.description);
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.section
          key="glossary"
          id={id}
          aria-labelledby={`${id}-title`}
          initial={{ height: 0, opacity: 0, marginTop: GRID_GAP }}
          animate={{ height: "auto", opacity: 1, marginTop: "0rem" }}
          exit={{ height: 0, opacity: 0, marginTop: GRID_GAP }}
          transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
          className={`overflow-hidden ${className}`}
        >
          <div className="rounded-[14px] bg-card p-5 shadow-card sm:p-7">
            <div className="max-w-2xl">
              <h2 id={`${id}-title`} className="text-lg font-semibold tracking-tight">
                Типы сущностей
              </h2>
              <p className="mt-1 text-sm text-muted">
                Каждой извлечённой сущности назначается один из этих типов. Цвет точки — тот же, что на графе.
              </p>
            </div>
            <dl className="mt-6 gap-x-10 sm:columns-2 xl:columns-3">
              {entries.map((t) => (
                <div key={t.type} className="mb-5 break-inside-avoid">
                  <dt className="flex items-center gap-2.5 font-medium">
                    <span
                      className="size-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: catalog.typeColor(t.type) }}
                      aria-hidden="true"
                    />
                    {t.label}
                  </dt>
                  <dd className="mt-1 pl-5 font-serif text-[14px] leading-[1.55] text-muted">{t.description}</dd>
                </div>
              ))}
            </dl>
          </div>
        </motion.section>
      )}
    </AnimatePresence>
  );
}
