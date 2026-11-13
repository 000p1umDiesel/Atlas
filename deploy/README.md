# Развёртывание mnogobase

Весь стек описан в одном `compose.yaml` в корне репозитория: API, worker, PostgreSQL, Qdrant, Neo4j (APOC + GDS) и три сервера моделей llama.cpp на CPU (embedder, reranker, vision). LLM подключается по внешнему API из `config.yaml`. Наружу публикуется только API на `127.0.0.1:8080`. На сервере перед ним стоит nginx с TLS.

Локальный запуск и запуск на сервере отличаются только тем, есть ли nginx. В `.env` лежат только секреты (`LLM_API_KEY`, пароли PostgreSQL и Neo4j, `QDRANT_API_KEY`), всё остальное настраивается в `config.yaml`, который монтируется в контейнеры. Данные хранятся в `./data` рядом с `compose.yaml`.

## Локально (Mac или Linux, нужен Docker)

```bash
cp .env.example .env              # только секреты: LLM_API_KEY, пароли, QDRANT_API_KEY
deploy/prepare-data.sh            # каталоги ./data/* и закреплённый jar GDS
docker compose up -d --build --wait
docker compose exec api mnogobase user create owner          # спросит пароль (от 12 символов)
uv run mnogobase client login owner                           # http://127.0.0.1:8080 по умолчанию
uv run mnogobase client documents upload paper.pdf
uv run mnogobase client jobs list
uv run mnogobase client ask "О чём статья?"
```

Первый старт скачивает GGUF-модели (несколько ГБ) в `data/models`, поэтому `--wait` может занять до получаса. Документы можно также класть в `data/app/inbox`: worker подхватит их сам.

Для разработки без контейнеров приложения: `docker compose up -d postgres qdrant neo4j embed rerank vision` поднимает только базы и модели, после этого работают обычные `uv run mnogobase ingest / ask / api serve`. Все порты открыты только на `127.0.0.1`, адреса совпадают с `config.yaml`. Сайты баз: Neo4j — http://localhost:7474 (пользователь `neo4j`, `NEO4J_PASSWORD`), Qdrant — http://localhost:6333/dashboard (`QDRANT_API_KEY`). Корпус пишет кто-то один — нативный CLI или worker в Docker (README, раздел 11).

## Сервер (Ubuntu, один раз)

```bash
# 1. Пакеты и firewall
sudo apt-get install -y docker.io docker-compose-v2 nginx certbot restic fail2ban
sudo ufw allow 22,80,443/tcp && sudo ufw enable

# 2. Код, секреты, каталоги данных (/srv/mnogobase — отдельный смонтированный том,
#    поэтому и /srv/mnogobase/app/data лежит на нём)
sudo git clone <repo> /srv/mnogobase/app && cd /srv/mnogobase/app
sudo cp .env.example .env && sudo nano .env       # только секреты: openssl rand -hex 24
echo MB_LOG_DRIVER=journald | sudo tee -a .env     # логи контейнеров — в журнал хоста
sudo deploy/prepare-data.sh

# 3. Запуск: стек поднимает systemd-юнит (им управляет host-mode)
sudo docker compose build
sudo cp deploy/mnogobase.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo host-mode mnogobase                          # внутри: systemctl start mnogobase
sudo docker compose exec api mnogobase user create owner

# 4. HTTPS: сертификат (nginx на минуту останавливается и при продлении), затем конфиг
sudo certbot certonly --standalone -d kb.example.org \
    --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
sudo sed 's/SERVER_NAME/kb.example.org/g' deploy/nginx.conf | sudo tee /etc/nginx/conf.d/mnogobase.conf
sudo nginx -t && sudo systemctl reload nginx

# 5. fail2ban: бан IP за подбор пароля по журналу nginx из шага 4
sudo cp deploy/fail2ban/mnogobase-login.filter.conf /etc/fail2ban/filter.d/mnogobase-login.conf
sudo cp deploy/fail2ban/mnogobase-login.jail.local /etc/fail2ban/jail.d/mnogobase-login.local
sudo fail2ban-client -t && sudo fail2ban-client reload

# 6. Бэкапы: restic-репозиторий вне сервера (S3, B2, SFTP)
echo 'sftp:backup@other-host:/srv/restic/mnogobase' | sudo tee /root/.restic-repo
openssl rand -hex 32 | sudo tee /root/.restic-pass && sudo chmod 600 /root/.restic-*
sudo restic -r "$(sudo cat /root/.restic-repo)" -p /root/.restic-pass init
sudo cp deploy/mnogobase-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mnogobase-backup.timer
```

Без домена подойдёт сертификат на IP: `certbot ... --preferred-profile shortlived -d <IP>`. Он короткоживущий и продлевается тем же таймером certbot.

С Mac: `mnogobase client --url https://kb.example.org login owner`. Адрес можно сохранить в `config.yaml` → `service.api_url` или в переменной `MNOGOBASE_API_URL`.

## Повседневные операции

| Что | Команда (из `/srv/mnogobase/app`) |
|---|---|
| Обновление | `sudo deploy/update.sh` — `git pull` ветки `main` и сборка `mnogobase:latest`, пока сайт работает; затем одна остановка: снимок restic и сразу запуск api и worker с новым образом. Другая ветка: `sudo deploy/update.sh <ветка>`, без снимка: `--no-backup`. Если ветки нет, она не перематывается (fast-forward), есть локальные правки или сборка упала, стек не трогается; если образ не изменился, снимка и перезапуска нет. git работает от владельца каталога, не от root; в режиме Minecraft стек не запускается |
| Откат | `update.sh` сохраняет заменяемый образ как `mnogobase:<его коммит>` (метка `org.opencontainers.image.revision`) и в конце печатает команду отката: `git checkout <коммит>`, `docker tag mnogobase:<коммит> mnogobase:latest`, `docker compose up -d --wait`. Миграции базы назад не откатываются: если они были, восстановите снимок restic |
| Minecraft ↔ mnogobase | `sudo host-mode vanilla` (или `neoforge`) / `sudo host-mode mnogobase`; `sudo host-mode status` |
| Состояние | `sudo docker compose ps`, `mnogobase client status` |
| Логи | `sudo docker compose logs -f api worker`, `journalctl -u mnogobase` |
| Операторские команды | `sudo docker compose exec api mnogobase status / doctor / graph analyze / reindex / jobs list` |
| Пользователь | `sudo docker compose exec api mnogobase user passwd owner` / `user disable NAME` |
| Баны за подбор пароля | `sudo fail2ban-client status mnogobase-login`, снять: `sudo fail2ban-client set mnogobase-login unbanip IP` |

Схема базы создаётся при старте (`CREATE ... IF NOT EXISTS`), затем применяются версионные миграции (`schema_migrations`). Они удаляют только то, что код больше не читает (например, колонку `users.role` и таблицу очереди `write_state`), но бэкап на этот случай уже входит в команду обновления. Если в `config.yaml` на сервере остался ключ `database.backend`, удалите его: с ним конфиг не проходит проверку и контейнеры не стартуют.

Никогда не используйте `docker compose down -v` и `docker volume prune`: данные лежат в bind-каталогах `data/`, но привычка опасна.

## Бэкап и проверка восстановления

`deploy/backup.sh` останавливает api, worker и базы на несколько минут (на медленном диске Neo4j поднимается 2–3 минуты). Он делает `restic backup` каталогов `data/{app,postgres,qdrant,neo4j}`, `.env` и `config.yaml`, а затем снова запускает то, что работало. Хранятся 7 ежедневных, 4 еженедельных и 6 ежемесячных снимков.

Раз в неделю: `sudo restic -r "$(sudo cat /root/.restic-repo)" -p /root/.restic-pass check`.

Учебное восстановление, не трогая рабочий стек:

```bash
sudo restic -r "$(sudo cat /root/.restic-repo)" -p /root/.restic-pass restore latest --target /tmp/restore
cd /srv/mnogobase/app
R=/tmp/restore/srv/mnogobase/app/data
sudo ln -s /srv/mnogobase/app/data/models $R/models      # модели не в бэкапе: берём рабочие
sudo deploy/prepare-data.sh $R                             # плагин GDS и пустые кэши
sudo MB_DATA=$R MB_BIND=127.0.0.2 docker compose -p mb-restore up -d --wait   # свои порты рядом с рабочим стеком
sudo docker compose -p mb-restore exec api mnogobase status
sudo docker compose -p mb-restore down && sudo rm -rf /tmp/restore
```

## Посмотреть базы на сервере

Сайты Neo4j и Qdrant открыты только на самом сервере. С Mac до них дотягивается SSH-туннель — зашифрованный проход через ваш SSH-ключ, из интернета эти страницы не видны:

```bash
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 -L 6333:localhost:6333 admin@<сервер>
```

Пока это окно открыто, на Mac работают http://localhost:7474 (Neo4j, пользователь `neo4j`, `NEO4J_PASSWORD` с сервера) и http://localhost:6333/dashboard (Qdrant, `QDRANT_API_KEY` с сервера). Локальный стек на Mac на это время остановите: он занимает те же порты. PostgreSQL — так же через `-L 5432:localhost:5432` и TablePlus / DBeaver / `psql`.

## Защита

- Все порты (API, базы, модели) открыты только на `127.0.0.1`: из интернета к ним не попасть, наружу смотрит лишь nginx на 80/443. Программы самого сервера базы видят, поэтому Qdrant закрыт API-ключом, Neo4j и PostgreSQL — длинными паролями из `.env`.
- У каждого контейнера есть лимиты памяти и CPU. Логи на сервере идут в journald (`MB_LOG_DRIVER=journald` в `.env`): там их ограничивает политика хоста, читает `docker compose logs` и `journalctl CONTAINER_NAME=mnogobase-kb-api-1`.
- Образы закреплены по версиям, плагин GDS проверяется по sha256.
- nginx: TLS 1.2/1.3, ответ 444 на чужой Host, лимит тела 64 KiB (50 МБ только для загрузки документа), предел частоты на `/api/auth/login`, журнал без строки запроса.
- fail2ban (`deploy/fail2ban/`): 3 неудачных входа (401 или 429 на `/api/auth/login` в журнале nginx) с одного IP за 7 дней — вечный бан на 80/443, SSH остаётся открытым. Трижды ошиблись паролем сами — снимите бан по SSH (команда в таблице выше). Журнал nginx, а не API: API за прокси видит адрес nginx, а не клиента.
- API: нет `/docs` и `/openapi.json`, пароли argon2 (не больше двух хешей одновременно), неудачный вход пишется в журнал как `login_failed` (без IP и с хешем имени). Пользователи создаются только из CLI.
