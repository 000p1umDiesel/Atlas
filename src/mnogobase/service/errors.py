"""Безопасные внешние ошибки прикладного сервиса.

Всё, что сервисный слой отдаёт наружу (HTTP API, CLI `user`), — это `ServiceError` с
HTTP-кодом и коротким текстом без DSN, SQL, путей и сообщений провайдеров. Внутренние
исключения переводятся в `ServiceError` там, где их ловят (`KnowledgeBase._with_app`,
`ServiceStore`); `api/app.py` превращает его в JSON-ответ `{"detail": ...}`.
"""


class ServiceError(Exception):
    """Ошибка, которую можно показать клиенту: `status` — HTTP-код, `detail` — текст.

    `detail` попадает в ответ API как есть, поэтому туда пишут только заранее известные
    безопасные строки, а не `str(exc)` чужого исключения."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


# Один предел длины вопроса для модели запроса API и для ядра (`KnowledgeBase`): 16000
# символов кириллицы — около 32 КиБ, это укладывается в предел тела запроса API (64 КиБ).
MAX_QUESTION_CHARS = 16000


def check_pagination(limit: int, offset: int) -> None:
    """Общая проверка `limit` / `offset` для списков API и хранилища (422 при ошибке)."""
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
        raise ServiceError(422, "Invalid pagination")
