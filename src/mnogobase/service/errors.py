"""Безопасные внешние ошибки прикладного сервиса."""


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)
