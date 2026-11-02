from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

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
    team: str | None
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
        if (
            not self.team
            or len(self.team) != 32
            or any(c not in "0123456789abcdef" for c in self.team)
        ):
            raise typer.BadParameter("Select a server team ID with client --team ID")
        return f"/api/teams/{self.team}/{suffix}"


def _default_api_url() -> str:
    from mnogobase.cli import _state
    from mnogobase.config import load_settings

    try:
        return load_settings(_state.get("config")).service.api_url
    except (OSError, ValueError) as exc:
        raise typer.BadParameter("Cannot load client configuration") from exc


def _client(ctx: typer.Context) -> Client:
    return ctx.find_object(Client)


def _print(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _id(value: str) -> str:
    return quote(value, safe="")


def register_client(app: typer.Typer) -> None:
    client_app = typer.Typer(no_args_is_help=True, help="HTTP client for the team service.")
    documents = typer.Typer(no_args_is_help=True)
    jobs = typer.Typer(no_args_is_help=True)
    chat = typer.Typer(no_args_is_help=True)
    wiki = typer.Typer(no_args_is_help=True)
    graph = typer.Typer(no_args_is_help=True)
    app.add_typer(client_app, name="client")
    for name, group in [
        ("documents", documents),
        ("jobs", jobs),
        ("chat", chat),
        ("wiki", wiki),
        ("graph", graph),
    ]:
        client_app.add_typer(group, name=name)

    @client_app.callback()
    def configure(
        ctx: typer.Context,
        url: str | None = typer.Option(None, "--url", envvar="MNOGOBASE_API_URL"),
        team: str | None = typer.Option(None, "--team", envvar="MNOGOBASE_TEAM"),
        credentials: Annotated[
            Path, typer.Option("--credentials", envvar="MNOGOBASE_CREDENTIALS")
        ] = DEFAULT_CREDENTIALS,
    ):
        url = url or _default_api_url()
        try:
            parsed = urlsplit(url)
            _ = parsed.port  # Validate ports as well as malformed bracketed hosts.
        except ValueError as exc:
            raise typer.BadParameter("Invalid API URL") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise typer.BadParameter(
                "Use an http(s) API URL without credentials, query or fragment"
            )
        ctx.obj = Client(url.rstrip("/"), team, Credentials(credentials))

    @client_app.command()
    def login(
        ctx: typer.Context,
        username: str,
        password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
    ):
        c = _client(ctx)
        result = c.request(
            "POST", "/api/auth/login", auth=False, json={"username": username, "password": password}
        ).json()
        c.credentials.save(c.url, result["token"])
        typer.echo("Logged in")

    @client_app.command()
    def logout(ctx: typer.Context):
        c = _client(ctx)
        c.request("POST", "/api/auth/logout")
        c.credentials.remove(c.url)
        typer.echo("Logged out")

    @client_app.command("teams")
    def teams_list(ctx: typer.Context):
        _print(_client(ctx).request("GET", "/api/teams").json())

    @documents.command("list")
    def documents_list(
        ctx: typer.Context,
        limit: int = typer.Option(50, min=1, max=200),
        offset: int = typer.Option(0, min=0),
    ):
        c = _client(ctx)
        _print(
            c.request("GET", c.path("documents"), params={"limit": limit, "offset": offset}).json()
        )

    @documents.command("upload")
    def documents_upload(
        ctx: typer.Context,
        path: Annotated[
            Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True)
        ],
    ):
        c = _client(ctx)
        with path.open("rb") as stream:
            _print(
                c.request(
                    "POST", c.path("documents"), params={"filename": path.name}, content=stream
                ).json()
            )

    @documents.command("remove")
    def documents_remove(ctx: typer.Context, doc_id: str):
        c = _client(ctx)
        _print(c.request("DELETE", c.path(f"documents/{_id(doc_id)}")).json())

    @documents.command("source")
    def documents_source(
        ctx: typer.Context, doc_id: str, output: Annotated[Path, typer.Option("--output", "-o")]
    ):
        c = _client(ctx)
        response = c.request("GET", c.path(f"documents/{_id(doc_id)}/content"))
        # Exclusive create avoids overwriting an existing local file or following a symlink.
        with output.open("xb") as stream:
            stream.write(response.content)
        typer.echo(str(output))

    @jobs.command("list")
    def jobs_list(
        ctx: typer.Context,
        limit: int = typer.Option(50, min=1, max=200),
        offset: int = typer.Option(0, min=0),
    ):
        c = _client(ctx)
        _print(c.request("GET", c.path("jobs"), params={"limit": limit, "offset": offset}).json())

    @jobs.command("retry")
    def jobs_retry(ctx: typer.Context, job_id: str):
        c = _client(ctx)
        _print(c.request("POST", c.path(f"jobs/{_id(job_id)}/retry")).json())

    @client_app.command("ask")
    def ask(ctx: typer.Context, question: str, mode: str = typer.Option("rag", "--mode", "-m")):
        c = _client(ctx)
        _print(c.request("POST", c.path("ask"), json={"question": question, "mode": mode}).json())

    @client_app.command("search")
    def search(
        ctx: typer.Context,
        question: str,
        mode: str = typer.Option("rag", "--mode", "-m"),
        k: int = typer.Option(8, min=1, max=100),
    ):
        c = _client(ctx)
        _print(
            c.request(
                "POST", c.path("search"), json={"question": question, "mode": mode, "k": k}
            ).json()
        )

    @chat.command("new")
    def chat_new(ctx: typer.Context, title: str = typer.Option("", "--title")):
        c = _client(ctx)
        _print(c.request("POST", c.path("conversations"), json={"title": title}).json())

    @chat.command("list")
    def chat_list(
        ctx: typer.Context,
        limit: int = typer.Option(50, min=1, max=200),
        offset: int = typer.Option(0, min=0),
    ):
        c = _client(ctx)
        _print(
            c.request(
                "GET", c.path("conversations"), params={"limit": limit, "offset": offset}
            ).json()
        )

    @chat.command("history")
    def chat_history(
        ctx: typer.Context,
        conversation_id: str,
        limit: int = typer.Option(100, min=1, max=200),
        offset: int = typer.Option(0, min=0),
    ):
        c = _client(ctx)
        _print(
            c.request(
                "GET",
                c.path(f"conversations/{_id(conversation_id)}/messages"),
                params={"limit": limit, "offset": offset},
            ).json()
        )

    @chat.command("send")
    def chat_send(
        ctx: typer.Context,
        conversation_id: str,
        question: str,
        mode: str = typer.Option("rag", "--mode", "-m"),
    ):
        c = _client(ctx)
        _print(
            c.request(
                "POST",
                c.path(f"conversations/{_id(conversation_id)}/messages"),
                json={"question": question, "mode": mode},
            ).json()
        )

    @chat.command("open")
    def chat_open(
        ctx: typer.Context,
        conversation_id: str | None = typer.Argument(None),
        title: str = typer.Option("", "--title"),
        mode: str = typer.Option("rag", "--mode", "-m"),
    ):
        c = _client(ctx)
        if conversation_id is None:
            conversation_id = c.request(
                "POST", c.path("conversations"), json={"title": title}
            ).json()["id"]
        typer.echo(f"Conversation {conversation_id}; /exit ends the session")
        _print(c.request("GET", c.path(f"conversations/{_id(conversation_id)}/messages")).json())
        while True:
            try:
                question = typer.prompt("You")
            except (EOFError, KeyboardInterrupt, typer.Abort):
                break
            if question.strip() in ("/exit", "/quit"):
                break
            if question.strip():
                _print(
                    c.request(
                        "POST",
                        c.path(f"conversations/{_id(conversation_id)}/messages"),
                        json={"question": question, "mode": mode},
                    ).json()
                )

    @wiki.command("list")
    def wiki_list(
        ctx: typer.Context,
        limit: int = typer.Option(50, min=1, max=200),
        offset: int = typer.Option(0, min=0),
    ):
        c = _client(ctx)
        _print(c.request("GET", c.path("wiki"), params={"limit": limit, "offset": offset}).json())

    @wiki.command("show")
    def wiki_show(ctx: typer.Context, page_id: str):
        c = _client(ctx)
        _print(c.request("GET", c.path(f"wiki/{_id(page_id)}")).json())

    @graph.command("search")
    def graph_search(ctx: typer.Context, query: str, limit: int = typer.Option(20, min=1, max=100)):
        c = _client(ctx)
        _print(c.request("GET", c.path("graph/search"), params={"q": query, "limit": limit}).json())

    @graph.command("neighbors")
    def graph_neighbors(
        ctx: typer.Context,
        entity_id: str,
        depth: int = typer.Option(1, min=1, max=3),
        limit: int = typer.Option(50, min=1, max=200),
    ):
        c = _client(ctx)
        _print(
            c.request(
                "GET",
                c.path(f"graph/{_id(entity_id)}/neighbors"),
                params={"depth": depth, "limit": limit},
            ).json()
        )
