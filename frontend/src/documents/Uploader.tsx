import { Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { ApiError } from "../api/client";
import { uploadFile, type UploadJob } from "../api/upload";

type Item = {
  key: number;
  file: File;
  status: "waiting" | "uploading" | "done" | "error";
  /** Доля отправленного тела, 0..1. */
  progress: number;
  /** Итог успешной загрузки: «принят», «уже в базе», «уже в очереди». */
  note?: string;
  error?: string;
};

/** Тот же файл уже загружали — показываем, где он, а не «принят». */
function noteOf(job: UploadJob): string {
  if (!job.duplicate) return "принят";
  return job.status === "done" ? "уже в базе" : "уже в очереди";
}

const LINK_BUTTON =
  "font-medium text-primary-ink underline underline-offset-4 hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary";

/**
 * Загрузка файлов: перетаскивание или выбор на диске. Файлы уходят строго по одному (медленный
 * диск сервера и общий лимит очереди); отказ сервера у одного файла не останавливает остальные.
 * Отмены загрузки нет — как в прежнем интерфейсе.
 */
export function Uploader({ onUploaded }: { onUploaded: () => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const nextKey = useRef(0);
  // Очередь ждущих и признак «идёт загрузка» — в ref, а не в эффекте от items: так StrictMode
  // (двойной запуск эффектов) не отправит файл дважды.
  const waiting = useRef<Item[]>([]);
  const running = useRef(false);
  const mounted = useRef(true);
  const uploaded = useRef(onUploaded);
  useEffect(() => {
    uploaded.current = onUploaded;
  }, [onUploaded]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      // Ушли со страницы: текущий файл догрузится, следующие не начинаются.
      mounted.current = false;
      waiting.current = [];
    };
  }, []);

  const patch = useCallback((key: number, change: Partial<Item>) => {
    setItems((list) => list.map((item) => (item.key === key ? { ...item, ...change } : item)));
  }, []);

  const pump = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      for (let next = waiting.current.shift(); next && mounted.current; next = waiting.current.shift()) {
        const { key, file } = next;
        patch(key, { status: "uploading" });
        try {
          const job = await uploadFile(file, (progress) => patch(key, { progress }));
          patch(key, { status: "done", progress: 1, note: noteOf(job) });
          uploaded.current();
        } catch (error) {
          patch(key, { status: "error", error: error instanceof ApiError ? error.detail : "Ошибка запроса" });
        }
      }
    } finally {
      running.current = false;
    }
  }, [patch]);

  const add = (files: FileList | File[] | null | undefined) => {
    const fresh = Array.from(files ?? []).map(
      (file): Item => ({ key: ++nextKey.current, file, status: "waiting", progress: 0 }),
    );
    if (!fresh.length) return;
    setItems((list) => [...list, ...fresh]);
    waiting.current.push(...fresh);
    void pump();
  };

  const onDragOver = (event: DragEvent) => {
    event.preventDefault();
    setOver(true);
  };
  const onDragLeave = (event: DragEvent) => {
    // уход на дочерний элемент зоны — не уход из зоны
    const next = event.relatedTarget as Node | null;
    if (next && event.currentTarget.contains(next)) return;
    setOver(false);
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setOver(false);
    add(event.dataTransfer?.files);
  };

  return (
    <section aria-label="Загрузка документов" className="space-y-3">
      <div
        data-dropzone=""
        data-over={over ? "true" : undefined}
        onDragOver={onDragOver}
        onDragEnter={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className="flex flex-col items-center gap-2 rounded-[14px] border-2 border-dashed border-line bg-card px-4 py-8 text-center transition-colors data-[over]:border-primary data-[over]:bg-primary/10 motion-reduce:transition-none"
      >
        <Upload size={22} className="text-primary-ink" aria-hidden="true" />
        <p>
          <strong className="font-semibold">Перетащите файлы сюда</strong> или{" "}
          <button type="button" className={LINK_BUTTON} onClick={() => input.current?.click()}>
            выберите на диске
          </button>
          .
        </p>
        <p className="max-w-prose text-sm text-muted">
          PDF, Word, PowerPoint, Markdown, HTML, картинки и другие форматы из настроек сервера. Файл появится в
          поиске, когда обработка закончится.
        </p>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            const files = Array.from(event.currentTarget.files ?? []);
            event.currentTarget.value = ""; // тот же файл можно выбрать снова
            add(files);
          }}
        />
      </div>
      {items.length > 0 && (
        <>
          <ul className="space-y-2">
            {items.map((item) => (
              <li
                key={item.key}
                data-status={item.status}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 rounded-xl bg-card px-3 py-2 text-sm shadow-card sm:grid-cols-[minmax(0,1fr)_minmax(6rem,12rem)_auto]"
              >
                <span className="min-w-0 truncate font-medium" title={item.file.name}>
                  {item.file.name}
                </span>
                {item.status === "error" ? (
                  <span className="col-span-2 text-danger sm:col-span-1 sm:col-start-2 sm:row-start-1 sm:text-right">
                    {item.error}
                  </span>
                ) : (
                  <progress
                    max={1}
                    value={item.progress}
                    aria-label={`Загрузка ${item.file.name}`}
                    className="col-span-2 h-1.5 w-full accent-primary sm:col-span-1 sm:col-start-2 sm:row-start-1"
                  />
                )}
                <span className="col-start-2 row-start-1 text-right text-muted tabular-nums sm:col-start-3">
                  {item.status === "waiting"
                    ? "ждёт"
                    : item.status === "uploading"
                      ? `${Math.round(item.progress * 100)} %`
                      : item.status === "done"
                        ? item.note
                        : ""}
                </span>
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={() => setItems((list) => list.filter((i) => i.status === "waiting" || i.status === "uploading"))}
            className="rounded-xl px-3 py-1.5 text-sm text-muted transition-colors hover:bg-ink/[0.06] hover:text-ink focus-visible:outline-2 focus-visible:outline-primary"
          >
            Очистить список
          </button>
        </>
      )}
    </section>
  );
}
