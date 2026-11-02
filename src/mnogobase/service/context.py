"""Неизменяемый контекст доступа и отдельные настройки команды."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mnogobase.config import Settings
from mnogobase.service.errors import ServiceError


@dataclass(frozen=True)
class TeamContext:
    team_id: str
    user_id: str
    role: str


def validate_team_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ServiceError(400, "Invalid team ID")
    return value


def team_settings(base: Settings, team_id: str) -> Settings:
    """Копирует настройки без повторного чтения окружения или изменения cwd."""
    validate_team_id(team_id)
    scoped = base.model_copy(deep=True)
    root = scoped.service.root / "teams" / team_id
    scoped.data_dir = root / "state"
    scoped.wiki.dir = root / "wiki"
    scoped.runs_dir = root / "runs"
    scoped.logs_dir = root / "logs"
    scoped.qdrant.prefix = f"mb_team_{team_id}_"
    scoped.neo4j.team_id = team_id
    scoped.database.project = f"team_{team_id}"
    return scoped
