from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Literal

import typer

from mnogobase.service.client import register_client
from mnogobase.service.errors import ServiceError
from mnogobase.service.store import ServiceStore


def _settings():
    # Resolve the existing global --config lazily; importing the legacy CLI here at
    # module import time would create a cycle when register_cli is called there.
    from mnogobase.cli import _settings as settings_loader

    return settings_loader()


def _store() -> ServiceStore:
    return ServiceStore(_settings().service.root / "service.db")


def _print(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _call(operation):
    try:
        return operation()
    except ServiceError as exc:
        typer.echo(exc.detail, err=True)
        raise typer.Exit(2) from exc


def register_cli(app: typer.Typer) -> None:
    """Добавляет операторские команды и HTTP-клиент к существующему CLI."""
    service_app = typer.Typer(no_args_is_help=True, help="Initialize and run the team service.")
    user_app = typer.Typer(no_args_is_help=True)
    team_app = typer.Typer(no_args_is_help=True)
    api_app = typer.Typer(no_args_is_help=True)
    for name, group in [
        ("service", service_app),
        ("user", user_app),
        ("team", team_app),
        ("api", api_app),
    ]:
        app.add_typer(group, name=name)
    register_client(app)

    @service_app.command("init")
    def initialize():
        _call(_store)
        typer.echo("Service initialized")

    @service_app.command("work")
    def work(
        once: bool = typer.Option(False, "--once"), interval: float = typer.Option(2.0, min=0.1)
    ):
        from mnogobase.service.runtime import TeamService

        async def run():
            service = TeamService(_settings())
            while True:
                count = await service.work_once()
                if once:
                    _print({"processed": count})
                    return
                if not count:
                    await asyncio.sleep(interval)

        with suppress(KeyboardInterrupt):
            _call(lambda: asyncio.run(run()))

    @user_app.command("create")
    def create_user(
        username: str,
        password: str = typer.Option(
            ..., "--password", prompt=True, hide_input=True, confirmation_prompt=True
        ),
    ):
        user = _call(lambda: _store().create_user(username, password))
        _print({key: user[key] for key in ("id", "username")})

    @team_app.command("create")
    def create_team(name: str):
        _print(_call(lambda: _store().create_team(name)))

    @team_app.command("list")
    def list_teams():
        _print(_call(lambda: _store().teams()))

    @team_app.command("add-member")
    def add_member(
        team_id: str,
        user_id: str,
        role: Literal["admin", "member"] = typer.Option("member", "--role"),
    ):
        _call(lambda: _store().add_member(team_id, user_id, role))
        typer.echo("Member added")

    @team_app.command("remove-member")
    def remove_member(team_id: str, user_id: str):
        _call(lambda: _store().remove_member(team_id, user_id))
        typer.echo("Member removed")

    @api_app.command("serve")
    def serve(
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8080, "--port", min=1, max=65535),
    ):
        import uvicorn

        from mnogobase.api.app import create_api

        uvicorn.run(create_api(_settings()), host=host, port=port, access_log=False)
