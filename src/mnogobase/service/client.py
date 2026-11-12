"""HTTP-клиент API mnogobase (`mnogobase client ...`) и локальное хранилище его токенов.

`mnogobase client login` получает bearer-токен через `/api/auth/login` и сохраняет его в
`~/.config/mnogobase/credentials.json` (по умолчанию) под адресом API; остальные команды
подставляют его в заголовок `Authorization`. Ошибки выводятся коротко в stderr с кодом
выхода 2; тело ответа сервера целиком не печатается.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer

DEFAULT_CREDENTIALS = Path.home() / ".config/mnogobase/credentials.json"


class Credentials:
    """Локальные opaque-токены: JSON `{адрес API без завершающего "/": токен}`.

    Файл перезаписывается атомарно (временный файл в том же каталоге, `fsync`, `replace`) и
    создаётся с правами 0600 (так создаёт `mkstemp`). Символическая ссылка на месте файла
    отвергается, чтобы токен нельзя было записать или прочитать через подменённый путь."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, str]:
        """Содержимое файла; отсутствующий файл — пустой словарь. Ссылка, нечитаемый или
        неверный по форме файл — `typer.BadParameter`."""
        if self.path.is_symlink():
            raise typer.BadParameter("Credential file must not be a symlink")
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in data.items()
            ):
                raise ValueError
            return data
        except (OSError, ValueError) as exc:
            raise typer.BadParameter("Cannot read credential file") from exc

    def _write(self, data: dict[str, str]) -> None:
        """Атомарно заменяет файл новым содержимым; временный файл удаляется при любом
        исходе (после успешного `replace` его уже нет)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".mnogobase-credentials-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def token(self, url: str) -> str | None:
        """Токен для адреса API или `None`."""
        return self._read().get(url.rstrip("/"))

    def save(self, url: str, token: str) -> None:
        """Сохраняет токен для адреса API, не трогая токены других адресов."""
        data = self._read()
        data[url.rstrip("/")] = token
        self._write(data)

    def remove(self, url: str) -> None:
        """Забывает токен адреса API (после `logout`)."""
        data = self._read()
        data.pop(url.rstrip("/"), None)
        self._write(data)


@dataclass
class Client:
    """Синхронный клиент одного адреса API с токеном из `credentials`."""

    url: str
    credentials: Credentials

    def request(self, method: str, path: str, *, auth: bool = True, **kwargs):
        """Выполняет запрос и возвращает `httpx.Response` с кодом < 300.

        С `auth` добавляет `Authorization: Bearer <токен>`; если токена нет —
        `typer.BadParameter`. `kwargs` передаются в `httpx` (`json`, `params`, `content`...).
        Перенаправления не выполняются: токен не должен уйти на другой адрес, а ответ 3xx
        считается ошибкой. Сетевая ошибка или код >= 300 печатают короткое сообщение в
        stderr (из ответа берётся только строковый `detail`, не длиннее 300 символов) и
        завершают команду с кодом 2 (`typer.Exit`). Таймаут 300 с — с запасом на `ask`."""
        headers = dict(kwargs.pop("headers", {}))
        if auth:
            token = self.credentials.token(self.url)
            if not token:
                raise typer.BadParameter("Run client login first")
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(base_url=self.url, timeout=300, follow_redirects=False) as client:
                response = client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            typer.echo("API connection failed", err=True)
            raise typer.Exit(2) from exc
        if response.status_code >= 300:
            # Тело ответа не печатаем: прокси или провайдер могут вернуть секреты или HTML.
            detail = "Request failed"
            try:
                value = response.json().get("detail")
                if isinstance(value, str):
                    detail = value[:300]
            except (ValueError, AttributeError):
                pass
            typer.echo(f"API {response.status_code}: {detail}", err=True)
            raise typer.Exit(2)
        return response

    def path(self, suffix: str) -> str:
        """Путь API по суффиксу: `path("jobs")` → `/api/jobs`."""
        return f"/api/{suffix}"
