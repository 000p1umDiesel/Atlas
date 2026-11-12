"""Команды `mnogobase client ...`: HTTP-клиент API базы знаний (локальной или на сервере).

Сам HTTP-клиент и хранение токенов — в `mnogobase.service.client`; здесь только команды.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import typer

from mnogobase.cli import common
from mnogobase.service.client import DEFAULT_CREDENTIALS, Client, Credentials


def _default_api_url() -> str:
    try:
        return common.load_settings(common.state.get("config")).service.api_url
    except (OSError, ValueError) as exc:
        raise typer.BadParameter("Cannot load client configuration") from exc


def _client(ctx: typer.Context) -> Client:
    return ctx.find_object(Client)


def _print(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _print_answer(value: dict, as_json: bool) -> None:
    """Ответ с цитатами в читаемом виде; `--json` — как есть."""
    if as_json:
        _print(value)
        return
    text = value.get("text") or value.get("content") or ""
    typer.echo(text)
    sources = value.get("sources") or []
    if sources:
        typer.echo("")
        for number, source in enumerate(sources, 1):
            where = source.get("path") or source.get("ref") or source.get("kind", "")
            page = f", p.{source['page']}" if source.get("page") is not None else ""
            link = f"  {source['url']}" if source.get("url") else ""
            typer.echo(f"[{number}] {where}{page}{link}")


def _id(value: str) -> str:
    return quote(value, safe="")


client_app = typer.Typer(
    no_args_is_help=True, help="HTTP client for the mnogobase API (local or on a server)."
)
documents = typer.Typer(no_args_is_help=True)
jobs = typer.Typer(no_args_is_help=True)
chat = typer.Typer(no_args_is_help=True)
wiki = typer.Typer(no_args_is_help=True)
graph = typer.Typer(no_args_is_help=True)
common.app.add_typer(client_app, name="client")
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
        raise typer.BadParameter("Use an http(s) API URL without credentials, query or fragment")
    ctx.obj = Client(url.rstrip("/"), Credentials(credentials))


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


@client_app.command("status")
def status(ctx: typer.Context):
    c = _client(ctx)
    _print(c.request("GET", c.path("status")).json())


@documents.command("list")
def documents_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
):
    c = _client(ctx)
    _print(c.request("GET", c.path("documents"), params={"limit": limit, "offset": offset}).json())


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
def ask(
    ctx: typer.Context,
    question: str,
    mode: str = typer.Option("rag", "--mode", "-m"),
    as_json: bool = typer.Option(False, "--json"),
):
    c = _client(ctx)
    _print_answer(
        c.request("POST", c.path("ask"), json={"question": question, "mode": mode}).json(),
        as_json,
    )


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
        c.request("GET", c.path("conversations"), params={"limit": limit, "offset": offset}).json()
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
    as_json: bool = typer.Option(False, "--json"),
):
    c = _client(ctx)
    _print_answer(
        c.request(
            "POST",
            c.path(f"conversations/{_id(conversation_id)}/messages"),
            json={"question": question, "mode": mode},
        ).json(),
        as_json,
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
        conversation_id = c.request("POST", c.path("conversations"), json={"title": title}).json()[
            "id"
        ]
    typer.echo(f"Conversation {conversation_id}; /exit ends the session")
    for message in c.request(
        "GET", c.path(f"conversations/{_id(conversation_id)}/messages")
    ).json():
        typer.echo(f"{message['role']}: {message['content']}")
    while True:
        try:
            question = typer.prompt("You")
        except (EOFError, KeyboardInterrupt, typer.Abort):
            break
        if question.strip() in ("/exit", "/quit"):
            break
        if question.strip():
            _print_answer(
                c.request(
                    "POST",
                    c.path(f"conversations/{_id(conversation_id)}/messages"),
                    json={"question": question, "mode": mode},
                ).json(),
                False,
            )
            typer.echo("")


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
