from __future__ import annotations

import json

import typer

from mnogobase.database import DatabaseError, database_target
from mnogobase.service.client import register_client
from mnogobase.service.errors import ServiceError
from mnogobase.service.store import ServiceStore


def _settings():
    # Resolve the existing global --config lazily; importing the CLI here at
    # module import time would create a cycle when register_cli is called there.
    from mnogobase.cli import _settings as settings_loader

    return settings_loader()


def _store() -> ServiceStore:
    return ServiceStore(database_target(_settings()))


def _print(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _call(operation):
    try:
        return operation()
    except DatabaseError:
        typer.echo("PostgreSQL unavailable; check database configuration", err=True)
        raise typer.Exit(2) from None
    except ServiceError as exc:
        typer.echo(exc.detail, err=True)
        raise typer.Exit(2) from exc


def _password_option():
    return typer.Option(..., "--password", prompt=True, hide_input=True, confirmation_prompt=True)


def register_cli(app: typer.Typer) -> None:
    """Добавляет учётные записи, HTTP API и его клиент к основному CLI."""
    user_app = typer.Typer(
        no_args_is_help=True, help="API accounts (created only here, never over HTTP)."
    )
    api_app = typer.Typer(no_args_is_help=True, help="HTTP API for the knowledge base.")
    app.add_typer(user_app, name="user")
    app.add_typer(api_app, name="api")
    register_client(app)

    @user_app.command("create")
    def create_user(username: str, password: str = _password_option()):
        user = _call(lambda: _store().create_user(username, password))
        _print({key: user[key] for key in ("id", "username")})

    @user_app.command("passwd")
    def change_password(username: str, password: str = _password_option()):
        """Set a new password; existing sessions of the user are revoked."""
        _call(lambda: _store().set_password(username, password))
        typer.echo("Password changed; sessions revoked")

    @user_app.command("disable")
    def disable_user(username: str):
        _call(lambda: _store().set_active(username, False))
        typer.echo("User disabled; sessions revoked")

    @user_app.command("enable")
    def enable_user(username: str):
        _call(lambda: _store().set_active(username, True))
        typer.echo("User enabled")

    @user_app.command("list")
    def list_users():
        _print(_call(lambda: _store().users()))

    @api_app.command("serve")
    def serve(
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8080, "--port", min=1, max=65535),
    ):
        import uvicorn

        from mnogobase.api.app import create_api

        uvicorn.run(
            create_api(_settings()),
            host=host,
            port=port,
            access_log=False,
            server_header=False,
        )
