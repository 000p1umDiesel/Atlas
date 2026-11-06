from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal

import typer

from mnogobase.database import DatabaseError, database_target
from mnogobase.service.client import register_client
from mnogobase.service.context import validate_team_id
from mnogobase.service.errors import ServiceError
from mnogobase.service.observability import ObservabilityStore
from mnogobase.service.store import ServiceStore


def _settings():
    # Resolve the existing global --config lazily; importing the legacy CLI here at
    # module import time would create a cycle when register_cli is called there.
    from mnogobase.cli import _settings as settings_loader

    return settings_loader()


def _store() -> ServiceStore:
    return ServiceStore(database_target(_settings(), "service"))


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

    def telemetry():
        settings = _settings()
        ServiceStore(database_target(settings, "service"))
        return ObservabilityStore(database_target(settings, "service"))

    @service_app.command("usage")
    def usage(
        team: str | None = typer.Option(None, "--team"),
        since: str | None = typer.Option(None, "--since"),
        until: str | None = typer.Option(None, "--until"),
    ):
        """Token totals and estimated external LLM costs (JSON, UTC period)."""

        def run():
            if team is not None:
                validate_team_id(team)

            def timestamp(value):
                if value is None:
                    return None
                try:
                    parsed = datetime.fromisoformat(value)
                    return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
                except (ValueError, OverflowError):
                    raise ServiceError(400, "Use an ISO date or datetime for the period") from None

            start, end = timestamp(since), timestamp(until)
            if start is not None and end is not None and start >= end:
                raise ServiceError(400, "--since must be earlier than --until")
            return telemetry().usage(team_id=team, since=start, until=end)

        _print(_call(run))

    @service_app.command("alerts")
    def alerts(
        team: str | None = typer.Option(None, "--team"),
        history: bool = typer.Option(False, "--history"),
        limit: int = typer.Option(100, min=1, max=1000),
        offset: int = typer.Option(0, min=0),
    ):
        """Active diagnostic events, or their full history (JSON)."""

        def run():
            if team is not None:
                validate_team_id(team)
            return telemetry().alerts(team_id=team, history=history, limit=limit, offset=offset)

        _print(_call(run))

    @service_app.command("monitor")
    def monitor(
        once: bool = typer.Option(False, "--once"),
        interval: float = typer.Option(30.0, min=0.1),
    ):
        """Detect failures independently of the worker; never retry jobs."""
        from mnogobase.service.monitor import ServiceMonitor

        async def run():
            watcher = ServiceMonitor(_settings())
            while True:
                _print(await asyncio.to_thread(watcher.scan))
                if once:
                    return
                await asyncio.sleep(interval)

        with suppress(KeyboardInterrupt):
            _call(lambda: asyncio.run(run()))

    @service_app.command("work")
    def work(
        once: bool = typer.Option(False, "--once"), interval: float = typer.Option(2.0, min=0.1)
    ):
        from mnogobase.service.runtime import TeamService

        async def run():
            service = TeamService(_settings())
            if not once and interval >= service.settings.service.monitor.worker_stale_s:
                raise ServiceError(
                    400, "Worker interval must be less than service.monitor.worker_stale_s"
                )
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
