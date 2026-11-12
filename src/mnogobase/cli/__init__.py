"""Командная строка `mnogobase` (точка входа `mnogobase.cli:app`).

Корневое приложение и общие помощники — в `common`. Каждый модуль пакета регистрирует свои
команды при импорте: корневые команды через `@common.app.command()`, группы через
`common.app.add_typer(...)`. Порядок импорта ниже задаёт порядок команд в `--help`.

| модуль       | команды                                          |
|--------------|--------------------------------------------------|
| `corpus`     | ingest, remove, wiki build                       |
| `query`      | ask, compare                                     |
| `project`    | doctor, status, reindex, reset                   |
| `graph`      | graph analyze / status / clear                   |
| `models`     | models up / down / status / logs                 |
| `jobs`       | jobs enqueue / list / retry, worker run          |
| `evaluation` | eval run / score / report / ingest / benchmark   |
| `users`      | user ..., api serve                              |
| `client`     | client ... (HTTP-клиент API)                     |

Импорт пакета ничего не подключает: ни один модуль не создаёт клиентов моделей и соединений с
базами при импорте. `App` строится только внутри команды через `common.build_app`, поэтому
`mnogobase --help` работает и без запущенных сервисов.
"""

# Порядок импорта важен (порядок в --help), поэтому сортировка импортов здесь отключена.
from mnogobase.cli import (  # noqa: F401, I001 — импорт регистрирует команды
    common,
    corpus,
    query,
    project,
    graph,
    models,
    jobs,
    evaluation,
    users,
    client,
)

app = common.app
