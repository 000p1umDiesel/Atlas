"""Наблюдение за папками (`worker run --watch DIR`): новые и изменённые файлы в очередь.

Сделано опросом, а не событиями файловой системы: опрос не теряет изменений, пропущенных
во время простоя или переполнения очереди событий, и не требует платформенных
зависимостей. Watcher работает в отдельном потоке worker'а со своим `JobQueue` и только
ставит задачи `ingest`; удалённые из папки файлы он из корпуса не убирает (для этого есть
`mnogobase remove --missing` и удаление через API).

Файл ставится в очередь, когда он «успокоился»: размер и `mtime` не менялись `settle`
секунд, и не изменились за время подсчёта хеша. Версия задачи — `file_doc_id` (sha256
содержимого), поэтому повторная постановка того же содержимого ничего не создаёт, а
источник, удалённый через API, без `force` не возвращается (см. `JobQueue.enqueue`).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mnogobase.core.ids import file_doc_id

from .queue import JobQueue


def discover(paths, extensions, excluded=()):
    """Находит файлы для загрузки и возвращает отсортированный список абсолютных путей.

    `paths` — файлы и каталоги (каталоги обходятся рекурсивно), `extensions` — допустимые
    расширения в любом регистре, с точкой или без, `excluded` — каталоги, которые не
    обходятся (служебные каталоги проекта: `data_dir`, wiki, логи, `runs`). Пропускаются
    скрытые файлы и каталоги (начинаются с `.`) и символические ссылки: watcher не должен
    уходить по ссылке за пределы наблюдаемой папки. Явно переданный файл не проверяется на
    скрытость, но тоже должен иметь подходящее расширение."""
    extensions = {"." + e.lower().lstrip(".") for e in extensions}
    excluded = [Path(p).resolve() for p in excluded]
    found = set()
    for path in paths:
        root = Path(path).resolve()
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [
                    d
                    for d in dirs
                    if not d.startswith(".")
                    and not any((Path(directory) / d).resolve().is_relative_to(p) for p in excluded)
                ]
                candidates.extend(Path(directory) / f for f in files if not f.startswith("."))
        for file in candidates:
            resolved = file.resolve()
            if (
                not file.is_symlink()
                and file.is_file()
                and file.suffix.lower() in extensions
                and not any(resolved.is_relative_to(p) for p in excluded)
            ):
                found.add(resolved)
    return sorted(found)


class Watcher:
    """Опрашивает `roots` и ставит в `queue` файлы, которые изменились и успокоились.

    Состояние в памяти: `_seen` — для каждого файла последняя сигнатура `(size, mtime_ns)` и
    момент, когда она появилась; `_submitted` — сигнатура, с которой файл уже поставлен. После
    рестарта память пуста, и все файлы ставятся снова; дубликаты отсекает сама очередь
    (та же пара путь + хеш)."""

    def __init__(
        self, queue: JobQueue, roots, extensions, *, settle=2.0, excluded=(), clock=time.monotonic
    ):
        """`settle` — сколько секунд сигнатура файла должна не меняться перед постановкой,
        `clock` — монотонные часы (подменяются в тестах); остальное как у `discover`."""
        self.queue, self.roots, self.extensions = queue, roots, extensions
        self.settle, self.excluded, self.clock = settle, excluded, clock
        self._seen = {}
        self._submitted = {}

    def scan(self) -> int:
        """Один проход опроса; возвращает число поставленных задач.

        Для каждого найденного файла читает хеш содержимого (полное чтение файла), но только
        если сигнатура не менялась `settle` секунд и с ней файл ещё не ставился. Если файл
        изменился, пока считался хеш, он остаётся до следующего прохода. Записи об исчезнувших
        файлах забываются. Ошибки очереди (база) пробрасываются вызывающему."""
        now, enqueued, live = self.clock(), 0, set()
        for path in discover(self.roots, self.extensions, self.excluded):
            live.add(path)
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                previous = self._seen.get(path)
                if previous is None or previous[0] != signature:
                    self._seen[path] = (signature, now)
                if (
                    now - self._seen[path][1] < self.settle
                    or self._submitted.get(path) == signature
                ):
                    continue
                digest = file_doc_id(path)
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != signature:
                    continue
                self.queue.enqueue(path, digest)
                self._submitted[path] = signature
                enqueued += 1
            except OSError:
                # Файл могли переименовать или удалить между обходом и чтением хеша.
                continue
        self._seen = {p: v for p, v in self._seen.items() if p in live}
        self._submitted = {p: v for p, v in self._submitted.items() if p in live}
        return enqueued
