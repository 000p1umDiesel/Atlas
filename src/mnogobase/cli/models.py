"""Локальные серверы моделей llama.cpp для разработки (`models up | down | status | logs`).

Касается только ролей с `serve: llamacpp`; роли `serve: api` работают на внешних серверах.
В Docker те же серверы — сервисы compose, и эти команды там не нужны.

Роли: embedder (порт 8001), rerank (8002), vision (8003) и llm (8000). Для каждой роли
`NativeRunner` запускает отдельный процесс `llama-server` в фоне: модель берётся из
`model`/`gguf` роли (GGUF скачивается при первом запуске), имя для API передаётся как
`--alias`, с `device: cpu` добавляется `--n-gpu-layers 0`. Вывод каждого сервера пишется в
`<logs_dir>/models-<роль>.log`, его показывает `models logs`.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Sequence
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from mnogobase.cli import common
from mnogobase.ops.model_servers import (
    NativeRunner,
    Served,
    ServeError,
    bring_up,
    health_state,
    is_ready,
    make_runner,
    served_roles,
    server_args,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Local llama.cpp model servers from config.yaml (serve: llamacpp): up, down, status, logs.",
)
common.app.add_typer(app, name="models")

WAIT_REPORT_EVERY = 15.0  # секунд между строками о сервере, который ещё не готов

RolesArg = Annotated[
    list[str] | None,
    typer.Argument(help="Roles: embedder, rerank, vision, llm (default: all served)."),
]


def _models(roles: list[str] | None) -> tuple[list[Served], list[Served], NativeRunner]:
    """Загружает настройки и возвращает `(выбранные, все, раннер)`.

    Выбранные — роли из `roles` (все локальные, если не заданы); все — роли с
    `serve: llamacpp`, которые раннер не должен останавливать при частичном запуске.
    Неизвестная роль или ошибка конфигурации — выход с кодом 2."""
    settings = common.get_settings()
    try:
        everything = served_roles(settings)
        return served_roles(settings, roles or ()), everything, make_runner(settings)
    except ServeError as exc:
        common.fail(exc)
        raise AssertionError("unreachable") from exc


@app.command("up")
def models_up(
    roles: RolesArg = None,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for the servers (first start downloads models).")
    ] = 1800,
) -> None:
    """Поднять llama-server для ролей с `serve: llamacpp` и дождаться их готовности.

    Без аргументов поднимает все локальные роли, иначе только перечисленные; серверы
    других ролей не трогаются. Первый запуск скачивает GGUF-модели, поэтому ожидание по
    умолчанию до 30 минут; пока сервер не готов, раз в 15 секунд печатается его состояние.
    Код выхода 2, если llama-server нет в PATH, сервер упал или не успел подняться."""
    console = common.console
    served, everything, model_runner = _models(roles)
    if not served:
        console.print("Nothing to start: every role is serve: api (or disabled).")
        return
    for s in served:
        command = shlex.join(server_args(s, s.port, gpu=model_runner.gpu))
        console.print(
            f"{s.role} → {s.settings.url}: llama-server {command}",
            soft_wrap=True,
            highlight=False,
        )
    start = time.monotonic()
    shown: dict[str, float] = {}  # роль -> когда о ней печаталась строка ожидания

    def stamp() -> str:
        """Время с начала запуска в виде `[м:сс]` для строк о ходе ожидания."""
        minutes, seconds = divmod(int(time.monotonic() - start), 60)
        return f"[{minutes}:{seconds:02d}]"

    def waiting(pending: Sequence[Served], elapsed: float) -> None:
        """Колбэк `bring_up`, вызывается в цикле ожидания со списком ещё не готовых ролей.

        Печатает состояние каждой ожидающей роли не чаще раза в `WAIT_REPORT_EVERY` секунд,
        чтобы долгая загрузка модели не заливала терминал одинаковыми строками."""
        # роли, пропавшие из списка ожидания, уже готовы — сообщаем об этом один раз
        names = {s.role for s in pending}
        for role in [r for r in shown if r not in names]:
            console.print(f"{stamp()} {role}: ready", highlight=False)
            del shown[role]
        now = time.monotonic()
        for s in pending:
            if now - shown.get(s.role, -WAIT_REPORT_EVERY) >= WAIT_REPORT_EVERY:
                shown[s.role] = now
                console.print(
                    f"{stamp()} {s.role}: {health_state(s)} "
                    f"(server log: `mnogobase models logs {s.role} -f`)",
                    soft_wrap=True,
                    highlight=False,
                )

    try:
        # `models up vision` не трогает серверы других ролей из конфига
        bring_up(served, model_runner, timeout, waiting, keep=everything)
    except (ServeError, OSError) as exc:  # OSError: нет llama-server в PATH
        common.fail(exc)
    console.print(f"[green]{stamp()} All model servers are ready.[/green]", highlight=False)


@app.command("down")
def models_down() -> None:
    """Остановить локальные серверы моделей.

    Останавливаются все серверы, запущенные models up; скачанные модели остаются в кэше,
    и следующий запуск их не качает."""
    _, _, model_runner = _models(None)
    try:
        model_runner.stop()
    except (ServeError, OSError) as exc:
        common.fail(exc)
    common.console.print("Model servers stopped.")


@app.command("status")
def models_status() -> None:
    """Показать, где работает модель каждой роли и готов ли её сервер.

    Для локальных ролей проверяется готовность сервера (ready или down), роли с serve: api
    и выключенные роли только отмечаются как api и disabled."""
    settings = common.get_settings()
    table = Table("role", "serve")
    for column in ("model", "url"):
        table.add_column(column, overflow="fold")
    table.add_column("state")
    roles = [
        ("embedder", settings.embedder),
        ("rerank", settings.rerank),
        ("vision", settings.vision),
        ("llm", settings.llm),
    ]
    for role, s in roles:
        if not getattr(s, "enabled", True):
            state = "disabled"
        elif s.serve == "api":
            state = "api"
        else:
            state = "[green]ready[/green]" if is_ready(Served(role, s)) else "[red]down[/red]"
        table.add_row(role, s.serve, escape(s.weights), escape(s.url), state)
    common.console.print(table)


@app.command("logs")
def models_logs(
    role: Annotated[str, typer.Argument(help="embedder, rerank, vision or llm.")],
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing new lines.")
    ] = False,
) -> None:
    """Показать последние строки лога сервера роли.

    С -f продолжает печатать новые строки, пока не нажат Ctrl+C. Код выхода 2, если роль
    не обслуживается локально."""
    served, _, model_runner = _models([role])
    if not served:
        common.fail(ServeError(f"{role} is not served locally (serve: api or disabled)"))
    try:
        model_runner.logs(served[0], follow)
    except (ServeError, OSError) as exc:
        common.fail(exc)
