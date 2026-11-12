"""Сервисный слой для владельца базы: фасад HTTP API, пользователи, сессии, диалоги,
учёт вызовов LLM и HTTP-клиент.

`kb.KnowledgeBase` — операции API поверх ядра (`App`) и очереди задач; `store.ServiceStore` —
учётные записи, сессии и диалоги; `observability.UsageStore` — таблица `llm_usage`;
`client` — клиент для `mnogobase client`; `errors.ServiceError` — единственный тип ошибок,
которые покидают сервис. Все таблицы живут в схеме проекта PostgreSQL."""
