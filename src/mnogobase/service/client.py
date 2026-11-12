"""HTTP-клиент API mnogobase и локальное хранилище его токенов."""

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
    """Локальные opaque-токены; файл заменяется атомарно с правами 0600."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, str]:
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
        return self._read().get(url.rstrip("/"))

    def save(self, url: str, token: str) -> None:
        data = self._read()
        data[url.rstrip("/")] = token
        self._write(data)

    def remove(self, url: str) -> None:
        data = self._read()
        data.pop(url.rstrip("/"), None)
        self._write(data)


@dataclass
class Client:
    url: str
    credentials: Credentials

    def request(self, method: str, path: str, *, auth: bool = True, **kwargs):
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
            # Never print raw response bodies: proxies/providers may return secrets or HTML.
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
        return f"/api/{suffix}"
