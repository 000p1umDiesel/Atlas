"""Типы файлов сборки веб-интерфейса не зависят от системной таблицы `/etc/mime.types`."""

from __future__ import annotations

import subprocess
import sys

# Как в `python:3.12-slim`: системных таблиц MIME нет, известны только встроенные типы.
_SCRIPT = """
import mimetypes, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

mimetypes.knownfiles[:] = []
mimetypes.init()
assert mimetypes.guess_type("f.woff2")[0] is None, "в таблице уже есть woff2"

from fastapi.testclient import TestClient
from mnogobase.api.app import create_api
from mnogobase.core.config import Settings

dist = Path(tempfile.mkdtemp())
(dist / "assets").mkdir()
(dist / "assets" / "golos-abc.woff2").write_bytes(b"wOF2")
settings = Settings()
client = TestClient(create_api(settings, kb=SimpleNamespace(settings=settings), web_dir=dist))
response = client.get("/assets/golos-abc.woff2")
assert response.status_code == 200, response.status_code
print(response.headers["content-type"])
"""


def test_woff2_served_as_font_without_system_mime_table(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True, cwd=tmp_path, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "font/woff2"
