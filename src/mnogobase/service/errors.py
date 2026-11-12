"""Безопасные внешние ошибки прикладного сервиса."""


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def check_pagination(limit: int, offset: int) -> None:
    """Общая проверка `limit` / `offset` для списков API и хранилища (422 при ошибке)."""
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
        raise ServiceError(422, "Invalid pagination")
