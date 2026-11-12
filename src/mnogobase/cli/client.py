"""Команды `mnogobase client ...`: HTTP-клиент API базы знаний (локальной или на сервере).

Сам HTTP-клиент и хранение токенов — в `mnogobase.service.client`; здесь только команды.

Адрес API берётся из `--url`, затем из `MNOGOBASE_API_URL`, затем из `service.api_url`
конфига. `client login` сохраняет Bearer-токен в файл учётных данных (по умолчанию
`~/.config/mnogobase/credentials.json`, права 0600, отдельный токен на каждый адрес API);
остальные команды отправляют его в заголовке Authorization. Все команды, кроме `ask` и
`chat send/open` (они печатают ответ с источниками), выводят ответ сервера как JSON.

Ошибки: нет соединения или сервер вернул ошибку — короткое сообщение в stderr и код 2.
Тело ответа с ошибкой целиком не печатается (в нём могут быть секреты или HTML прокси),
только поле `detail`. Идентификаторы в путях URL экранируются (`_id`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import typer
import yaml

from mnogobase.cli import common
from mnogobase.service.client import DEFAULT_CREDENTIALS, Client, Credentials


def _default_api_url() -> str:
    """Адрес API из конфига (`service.api_url`), если не задан `--url` или переменная.

    Конфиг читается только здесь и только когда он нужен: с явным `--url` клиент работает
    и без config.yaml."""
    try:
        return common.load_settings(common.state.get("config")).service.api_url
    except (OSError, ValueError, yaml.YAMLError) as exc:  # нет файла, битый YAML, схема
        raise typer.BadParameter("Cannot load client configuration") from exc


def _client(ctx: typer.Context) -> Client:
    """`Client`, созданный callback'ом `configure` для этого вызова."""
    return ctx.find_object(Client)


def _print(value) -> None:
    """Печатает ответ сервера как JSON с отступами; кириллица не экранируется."""
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _print_answer(value: dict, as_json: bool) -> None:
    """Ответ с цитатами в читаемом виде; `--json` — как есть.

    Понимает и ответ `/api/ask` (поле `text`), и сообщение беседы (поле `content`). После
    текста печатается нумерованный список источников: путь (или ref), страница и ссылка."""
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
    """Экранирует идентификатор для пути URL целиком, включая `/`: значение вида `../x`
    не уводит запрос на другой адрес API."""
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
    """Общие параметры `client`: создаёт `Client` для подкоманды.

    Адрес проверяется до любого запроса: только http(s), с хостом и корректным портом, без
    логина и пароля в URL (они попали бы в историю оболочки и логи), без query и фрагмента.
    Неверный адрес — ошибка параметра (код 2)."""
    url = url or _default_api_url()
    try:
        parsed = urlsplit(url)
        _ = parsed.port  # чтение порта проверяет и сам порт, и хост в квадратных скобках
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
    """Войти в API и сохранить токен сессии.

    Пароль запрашивается скрыто. Токен сохраняется в файл учётных данных для этого адреса
    API и используется остальными командами."""
    c = _client(ctx)
    result = c.request(
        "POST", "/api/auth/login", auth=False, json={"username": username, "password": password}
    ).json()
    c.credentials.save(c.url, result["token"])
    typer.echo("Logged in")


@client_app.command()
def logout(ctx: typer.Context):
    """Выйти: отозвать сессию на сервере и удалить сохранённый токен."""
    c = _client(ctx)
    c.request("POST", "/api/auth/logout")
    c.credentials.remove(c.url)
    typer.echo("Logged out")


@client_app.command("status")
def status(ctx: typer.Context):
    """Показать состояние сервиса: очередь задач, жив ли worker и расход LLM."""
    c = _client(ctx)
    _print(c.request("GET", c.path("status")).json())


@documents.command("list")
def documents_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
):
    """Показать документы базы знаний постранично."""
    c = _client(ctx)
    _print(c.request("GET", c.path("documents"), params={"limit": limit, "offset": offset}).json())


@documents.command("upload")
def documents_upload(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True)
    ],
):
    """Загрузить файл в базу знаний.

    Файл передаётся потоком, без чтения целиком в память. Сервер сохраняет его и ставит
    задачу ingest, которую потом выполнит worker; в ответе — эта задача. Повторная
    загрузка того же содержимого возвращает уже существующую задачу."""
    c = _client(ctx)
    with path.open("rb") as stream:
        _print(
            c.request(
                "POST", c.path("documents"), params={"filename": path.name}, content=stream
            ).json()
        )


@documents.command("remove")
def documents_remove(ctx: typer.Context, doc_id: str):
    """Удалить документ из базы знаний по его doc_id.

    Сервер ставит задачу remove; знания документа исчезнут, когда её выполнит worker."""
    c = _client(ctx)
    _print(c.request("DELETE", c.path(f"documents/{_id(doc_id)}")).json())


@documents.command("source")
def documents_source(
    ctx: typer.Context, doc_id: str, output: Annotated[Path, typer.Option("--output", "-o")]
):
    """Скачать исходный файл документа в новый файл --output.

    Существующий файл не перезаписывается: команда завершится ошибкой."""
    c = _client(ctx)
    response = c.request("GET", c.path(f"documents/{_id(doc_id)}/content"))
    # режим "x" не перезапишет существующий файл и не пойдёт по символической ссылке
    with output.open("xb") as stream:
        stream.write(response.content)
    typer.echo(str(output))


@jobs.command("list")
def jobs_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
):
    """Показать задачи фоновой очереди постранично."""
    c = _client(ctx)
    _print(c.request("GET", c.path("jobs"), params={"limit": limit, "offset": offset}).json())


@jobs.command("retry")
def jobs_retry(ctx: typer.Context, job_id: str):
    """Вернуть упавшую задачу в очередь и показать её состояние."""
    c = _client(ctx)
    _print(c.request("POST", c.path(f"jobs/{_id(job_id)}/retry")).json())


@client_app.command("ask")
def ask(
    ctx: typer.Context,
    question: str,
    mode: str = typer.Option("rag", "--mode", "-m"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Задать вопрос базе знаний и получить ответ со ссылками на источники.

    Вопрос не сохраняется в беседу. С --json печатается полный ответ сервера."""
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
    """Найти фрагменты по вопросу без генерации ответа.

    Возвращает то, что retriever выбранного режима отдал бы в контекст LLM, в JSON."""
    c = _client(ctx)
    _print(
        c.request(
            "POST", c.path("search"), json={"question": question, "mode": mode, "k": k}
        ).json()
    )


@chat.command("new")
def chat_new(ctx: typer.Context, title: str = typer.Option("", "--title")):
    """Создать новую беседу и показать её id."""
    c = _client(ctx)
    _print(c.request("POST", c.path("conversations"), json={"title": title}).json())


@chat.command("list")
def chat_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
):
    """Показать свои беседы постранично."""
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
    """Показать сообщения беседы постранично."""
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
    """Отправить вопрос в беседу и показать ответ.

    Вопрос и ответ сохраняются в истории беседы."""
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
    """Открыть беседу в интерактивном режиме.

    Без id создаёт новую беседу. Печатает историю, затем принимает вопросы по одному;
    /exit, /quit, Ctrl+D или Ctrl+C завершают сеанс."""
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
    """Показать страницы wiki постранично."""
    c = _client(ctx)
    _print(c.request("GET", c.path("wiki"), params={"limit": limit, "offset": offset}).json())


@wiki.command("show")
def wiki_show(ctx: typer.Context, page_id: str):
    """Показать страницу wiki по её id."""
    c = _client(ctx)
    _print(c.request("GET", c.path(f"wiki/{_id(page_id)}")).json())


@graph.command("search")
def graph_search(ctx: typer.Context, query: str, limit: int = typer.Option(20, min=1, max=100)):
    """Найти сущности графа знаний полнотекстовым поиском."""
    c = _client(ctx)
    _print(c.request("GET", c.path("graph/search"), params={"q": query, "limit": limit}).json())


@graph.command("neighbors")
def graph_neighbors(
    ctx: typer.Context,
    entity_id: str,
    depth: int = typer.Option(1, min=1, max=3),
    limit: int = typer.Option(50, min=1, max=200),
):
    """Показать соседей сущности в графе знаний на глубину до 3 связей."""
    c = _client(ctx)
    _print(
        c.request(
            "GET",
            c.path(f"graph/{_id(entity_id)}/neighbors"),
            params={"depth": depth, "limit": limit},
        ).json()
    )
