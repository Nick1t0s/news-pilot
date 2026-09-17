# Деплой news-pilot на Ubuntu-сервер (Docker, БД на хосте)

Схема: приложение в Docker-контейнере, PostgreSQL 16 + pgvector — нативно на хосте.
Проверено на Ubuntu 24.04. Все команды от root (или с sudo).

## 1. Подготовка системы

```bash
apt update && apt upgrade -y
apt install -y ca-certificates curl git
```

## 2. Docker Engine + Compose

```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | tee /etc/apt/sources.list.d/docker.list >/dev/null
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker run --rm hello-world
```

## 3. PostgreSQL 16 + pgvector (на хосте, вне Docker)

```bash
apt install -y postgresql postgresql-16-pgvector    # на 22.04 сначала подключите PGDG-репо

su - postgres -c "psql -c \"CREATE ROLE news LOGIN PASSWORD 'СЛОЖНЫЙ_ПАРОЛЬ';\""
su - postgres -c "createdb -O news news"
# pgvector создаётся только от суперпользователя:
su - postgres -c "psql -d news -c \"CREATE EXTENSION IF NOT EXISTS vector;\""
```

Разрешить контейнерам доступ к хост-БД — в `/etc/postgresql/16/main/postgresql.conf`:

```conf
listen_addresses = 'localhost,172.17.0.1'    # docker-мост
```

В `/etc/postgresql/16/main/pg_hba.conf`:

```conf
host  all  all  172.16.0.0/12  scram-sha-256
```

```bash
systemctl restart postgresql
```

## 4. Деплой проекта

Проект держать в Linux-ФС (например `/opt/news-pilot`), не на сетевых/NTFS-монтах.

```bash
cd /opt
git clone <URL_РЕПОЗИТОРИЯ> news-pilot
cd news-pilot
cp .env.example .env && cp config.example.yaml config.yaml
```

**`.env`** — минимум:

```env
DATABASE__DSN=postgresql+asyncpg://news:СЛОЖНЫЙ_ПАРОЛЬ@host.docker.internal:5432/news
LLM__API_KEY=sk-...
TAVILY__API_KEY=tvly-...
TELEGRAM__BOT_TOKEN=123456:ABC...
```

`host.docker.internal` работает из коробки: compose маппит его на `host-gateway`
(172.17.0.1 — docker-мост хоста), поэтому в DSN именно он, не localhost.

**`config.yaml`** — заполнить под заказчика:

```yaml
llm:
  base_url: "<адрес OpenAI-compatible шлюза>"   # и model
embeddings:
  base_url: "http://ollama:11434"               # из контейнера localhost НЕ работает
telegram:
  channel_id: -100...                           # канал публикации (бот — админ с правом публикации)
  admin_id: 123...
rss:
  feeds: [...]                                  # ленты заказчика
publish:
  mode: auto | moderation
  footer_text: "👉 Подпишись на"                # подпись в конце поста (пусто = без)
  footer_label: "Info+"
  footer_url: "https://t.me/news_info_plus"
```

Прокси-поля (`proxy:`) из локального dev-конфига на сервере занулить/закомментировать,
если прямого доступа достаточно.

## 5. Запуск

```bash
docker compose up -d --build
docker compose --profile optional up -d ollama      # Ollama (эмбеддинги) — обязательна
docker compose exec ollama ollama pull qwen3-embedding:0.6b
docker compose logs -f app                          # ждать "news-pilot started: feeds=N ..."
```

Первый старт: схема БД и векторный индекс создаются автоматически;
`rss.clear_run: true` пометит уже имеющиеся ленты как `cleared` (без флуда старыми новостями).
Первый пост — после первого батча отбора (`batch_interval_seconds: 1800`, до 30 минут).

## 6. Проверка

1. `docker compose ps` — оба контейнера Up.
2. `/admin` у бота отвечает; бот добавлен админом канала («Публикация сообщений»).
3. Новый пост появился в канале после первого батча.

## 7. Эксплуатация

```bash
docker compose logs -f --since 5m app   # логи (только свежее)
docker compose restart                  # после правки config.yaml (монтируется :ro)
docker compose up -d --build            # после git pull (обновление кода)
su - postgres -c "pg_dump news" > news-backup-$(date +%F).sql   # бэкап
```

Автостарт после ребута обеспечен: docker.service включается пакетом автоматически,
контейнер — `restart: unless-stopped`. Отдельный systemd-юнит не нужен.

## 8. Траблшутинг

| Симптом | Причина / решение |
|---|---|
| `no pg_hba.conf entry for host "172.18.x.x"` | Нет правила для docker-сети: `host all all 172.16.0.0/12 scram-sha-256` в pg_hba + restart |
| `InvalidPasswordError` | Пароль роли не совпадает с DSN: `ALTER ROLE ... PASSWORD '...'` |
| `ConnectError` к `localhost:11434` | В config.yaml должно быть `http://ollama:11434`, не localhost |
| `extension "vector" is not available` | Нет пакета `postgresql-16-pgvector` |
| `need administrator rights in the channel chat` | Бот не админ канала (право «Публикация сообщений») |
| Ollama отвечает 400 `llama-server process no longer running` | OOM-killer убил llama-server — см. README «Траблшутинг»; env `OLLAMA_NUM_PARALLEL=1` и т.д. |
| LLM/Tavily/Telegram ошибки | Исходящий доступ/прокси — поля `proxy:` в config.yaml |
