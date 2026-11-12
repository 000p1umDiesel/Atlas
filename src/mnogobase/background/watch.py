"""Polling survives missed filesystem events and works without platform dependencies."""

from __future__ import annotations

import os
import time
from pathlib import Path

from mnogobase.core.ids import file_doc_id

from .queue import JobQueue


def discover(paths, extensions, excluded=()):
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
    def __init__(
        self, queue: JobQueue, roots, extensions, *, settle=2.0, excluded=(), clock=time.monotonic
    ):
        self.queue, self.roots, self.extensions = queue, roots, extensions
        self.settle, self.excluded, self.clock = settle, excluded, clock
        self._seen = {}
        self._submitted = {}

    def scan(self) -> int:
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
                # A writer can rename/remove a file between discovery and hashing.
                continue
        self._seen = {p: v for p, v in self._seen.items() if p in live}
        self._submitted = {p: v for p, v in self._submitted.items() if p in live}
        return enqueued
