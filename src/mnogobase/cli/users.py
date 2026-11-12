"""Учётные записи API (`user create | passwd | disable | enable | list`) и запуск HTTP API
(`api serve`).

Пользователей создаёт только владелец из командной строки: через HTTP это сделать нельзя.
Учётные записи хранятся в схеме проекта PostgreSQL (`ServiceStore`), пароли — хешами argon2.
Смена пароля и отключение пользователя сразу отзывают все его сессии. Ошибки базы и сервиса
печатаются в stderr коротким безопасным сообщением, код выхода 2.

`api serve` запускает FastAPI-приложение через uvicorn. API только читает базу и ставит
задачи в очередь; записывает корпус отдельный процесс `mnogobase worker run`.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from mnogobase.cli import common
from mnogobase.core.database import DatabaseError, database_target
from mnogobase.service.errors import ServiceError
from mnogobase.service.store import ServiceStore

user_app = typer.Typer(
    no_args_is_help=True, help="Учётные записи API (создаются только здесь, не через HTTP)"
)
api_app = typer.Typer(no_args_is_help=True, help="HTTP API базы знаний")
common.app.add_typer(user_app, name="user")
common.app.add_typer(api_app, name="api")


def _store() -> ServiceStore:
    """Хранилище пользователей в схеме текущего проекта (по настройкам из `--config`)."""
    return ServiceStore(database_target(common.get_settings()))


def _print(value) -> None:
    """Печатает значение как JSON с отступами; кириллица не экранируется."""
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _call(operation):
    """Выполняет операцию хранилища и возвращает её результат; ошибки БД и сервиса —
    сообщение в stderr и код 2.

    Для `DatabaseError` печатается общий текст: исходное сообщение может содержать адрес
    базы. `ServiceError.detail` уже безопасен для показа (например, «пользователь не
    найден»)."""
    try:
        return operation()
    except DatabaseError:
        typer.echo("PostgreSQL недоступен. Проверьте настройки базы данных.", err=True)
        raise typer.Exit(2) from None
    except ServiceError as exc:
        typer.echo(exc.detail, err=True)
        raise typer.Exit(2) from exc


def _password_option():
    """Пароль вводится скрыто и дважды; в истории оболочки он не остаётся."""
    return typer.Option(
        ...,
        "--password",
        prompt="Пароль",
        hide_input=True,
        confirmation_prompt="Повторите пароль",
        help="Пароль (не короче 12 символов); без опции запрашивается скрыто",
    )


@user_app.command("create")
def create_user(
    username: Annotated[str, typer.Argument(help="Имя пользователя")],
    password: str = _password_option(),
):
    """Создать учётную запись API.

    Пароль (не короче 12 символов) вводится скрыто и дважды. Печатает id и имя нового
    пользователя в JSON. Код выхода 2, если имя занято, пароль не подходит или PostgreSQL
    недоступен."""
    user = _call(lambda: _store().create_user(username, password))
    _print({key: user[key] for key in ("id", "username")})


@user_app.command("passwd")
def change_password(
    username: Annotated[str, typer.Argument(help="Имя пользователя")],
    password: str = _password_option(),
):
    """Сменить пароль пользователя; все его сессии отзываются.

    Новый пароль (не короче 12 символов) вводится скрыто и дважды."""
    _call(lambda: _store().set_password(username, password))
    typer.echo("Пароль изменён, сессии отозваны")


@user_app.command("disable")
def disable_user(username: Annotated[str, typer.Argument(help="Имя пользователя")]):
    """Отключить пользователя; все его сессии отзываются.

    Отключённый пользователь не может войти, пока его не включат снова (user enable)."""
    _call(lambda: _store().set_active(username, False))
    typer.echo("Пользователь отключён, сессии отозваны")


@user_app.command("enable")
def enable_user(username: Annotated[str, typer.Argument(help="Имя пользователя")]):
    """Снова включить отключённого пользователя."""
    _call(lambda: _store().set_active(username, True))
    typer.echo("Пользователь включён")


@user_app.command("list")
def list_users():
    """Показать всех пользователей (id, имя, активен ли) в JSON."""
    _print(_call(lambda: _store().users()))


@api_app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Адрес, на котором слушать"),
    port: int = typer.Option(8080, "--port", min=1, max=65535, help="Порт"),
):
    """Запустить HTTP API базы знаний.

    По умолчанию слушает только 127.0.0.1:8080; снаружи API открывают через reverse
    proxy с TLS (см. deploy/nginx.conf). API читает базу и ставит задачи в очередь, а
    записывает корпус отдельный процесс mnogobase worker run. Работает до Ctrl+C."""
    import uvicorn  # только для этой команды: остальным CLI веб-сервер не нужен

    from mnogobase.api.app import create_api

    uvicorn.run(
        create_api(common.get_settings()),
        host=host,
        port=port,
        access_log=False,
        server_header=False,
    )
