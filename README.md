# news-pilot

Агент ведения новостного Telegram-канала: демон опрашивает RSS-ленты, догружает полные тексты статей,
отсеивает дубликаты (pgvector + LLM), подбирает фото через ИИ-агента с инструментами, генерирует пост
в стиле канала (со ссылками на прошлые посты при развитии истории) и публикует его в Telegram.

HTTP-сервера в приложении нет: источник — RSS, мониторинг — логи (JSON) и админ-меню бота.

## Стек

Python 3.12, feedparser, trafilatura, PostgreSQL 16+ с pgvector (на хост-машине), asyncpg (raw SQL),
OpenAI-compatible LLM (openai SDK), Ollama (эмбеддинги), tavily-python, aiogram 3, pydantic-settings.

## Запуск

### Локально (для разработки)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env               # заполнить секреты
cp config.example.yaml config.yaml # настроить фиды/канал/режим

# БД — PostgreSQL на машине (16+, с расширением pgvector).
# Пользователь, пароль и база — любые; всё задаётся в .env (DATABASE__DSN).
sudo apt install postgresql postgresql-18-pgvector   # имя пакета pgvector зависит от версии PG
sudo -u postgres psql -c "CREATE ROLE мой_юзер LOGIN PASSWORD 'мой_пароль';"
sudo -u postgres createdb -O мой_юзер моя_база

# В .env указать (юзер/пароль/база — ваши):
# DATABASE__DSN=postgresql+asyncpg://мой_юзер:мой_пароль@localhost:5432/моя_база
# TEST_DSN=postgresql+asyncpg://мой_юзер:мой_пароль@localhost:5432/моя_база

# Ollama (эмбеддинги)
ollama pull qwen3-embedding:0.6b

.venv/bin/python main.py
```

Схема БД создаётся автоматически при старте (`app/db/schema.sql`, только `CREATE ... IF NOT EXISTS`).

### Docker

PostgreSQL в compose **нет** — требуется работающий PostgreSQL на хост-машине
(прослушивает localhost, юзер/база — из `DATABASE__DSN`).

```bash
cp .env.example .env && cp config.example.yaml config.yaml  # заполнить
docker compose up -d --build          # app (БД — на хосте)
docker compose --profile optional up -d ollama   # если нужен локальный Ollama
```

В контейнере БД доступна по `host.docker.internal:5432` (в compose добавлен `host-gateway`),
поэтому в `.env` укажите: `DATABASE__DSN=postgresql+asyncpg://юзер:пароль@host.docker.internal:5432/база`.
PostgreSQL должен слушать TCP (в `postgresql.conf`: `listen_addresses = 'localhost'` — по умолчанию
достаточно; для доступа из контейнера на Linux используйте `host-gateway`-адрес и правило
в `pg_hba.conf` для сети docker-моста, например `172.16.0.0/12`).

Фид эмулятора в конфиге приложения: `http://host.docker.internal:PORT/rss`
(в compose добавлен `host-gateway`).

## Конфигурация

- `config.yaml` — все параметры (см. `config.example.yaml`): llm, embeddings, rss, fetcher,
  database, tavily, telegram, dedup, photo_agent, context, publish, pipeline.
- `.env` — секреты, переопределяют YAML: `LLM__API_KEY`, `TAVILY__API_KEY`,
  `TELEGRAM__BOT_TOKEN`, `DATABASE__DSN` (вложенность — через `__`).
- Смена `publish.mode` (`auto` | `moderation`) — только конфиг + рестарт, код менять не нужно.
- `embeddings.dimensions` (1024) зашита в схему БД; при смене — пересоздать базу (данные не критичны).

## Пайплайн

```
RSS feeds → Poller → догрузка полного текста (trafilatura, fallback на summary)
  → статус pending → эмбеддинг (Ollama)
  → [1] Дедупликация: pgvector top-5 за window_days с порогом min_similarity + LLM-вердикт
      → duplicate / needs_review (on_error: review|pass|drop) — конец
  → [2] Фото-агент: tavily_image_search / inspect_image (vision) / select_images,
      лимиты max_iterations/max_searches/max_images; 0 фото — валидный исход
  → [3] Поиск релевантных опубликованных постов (top-3, context.window_days)
  → [4] Генерация поста по prompts/style.md (structured output, ≤1000 символов, HTML)
  → [5] Публикация: auto (rate-limit max_per_hour + quiet_hours, очередь)
       или moderation (черновик админу: Опубликовать / Редактировать / Отклонить, таймаут 24ч)
  → пост + эмбеддинг сохраняются в БД постов
```

Очередь — `asyncio.Queue` + воркеры (`pipeline.workers`). Сбой одного этапа помечает новость
`status=failed` и не роняет очередь. Сбой фото/Tavily не блокирует публикацию текстового поста.

## Статусы новости

`pending → dedup → (duplicate | needs_review | photo_search) → writing → (moderation | published)`,
`rejected` — отклонение на модерации или таймаут, `failed` — при ошибках этапа или коротком тексте.

Черновики постов хранятся в таблице `posts` со статусом `draft` (модерация) / `queued`
(очередь auto) и переотправляются при рестарте; в поиске связанных постов участвуют только
`status=published`. Отклонённые посты (вручную или по таймауту) удаляются из `posts`;
новость получает `status=rejected`.

## Эмулятор новостей (ручной e2e)

Не входит в docker-образ и compose. Запуск:

```bash
python emulator/main.py --scenario mixed --interval 60 --port 8080
```

- `GET /rss` — фид с новыми записями каждые `--interval` сек; `link` ведёт на страницу-заглушку
  с полным текстом (проверяется догрузка trafilatura).
- Сценарии `--scenario`: `random`, `duplicates` (переформулировки — проверка дедупликации),
  `developing` (продолжения тем — проверка ссылок на прошлые посты), `mixed`.
- В конфиг приложения: `- name: emulator, url: http://localhost:8080/rss`
  (или `http://host.docker.internal:8080/rss` при запуске приложения в docker).

## Бот

- Один админ: `telegram.admin_id`. Апдейты от остальных игнорируются.
- `/admin` — инлайн-меню: «📊 Статистика», «🔄 Обновить» (статистика считается SQL-запросами на лету).
- Модерация: черновик приходит в личку админу; кнопки «Опубликовать», «Редактировать»
  (прислать новый текст), «Отклонить». Таймаут (`publish.moderation_timeout_hours`, 24ч) → отклонение.

## Тесты

```bash
# нужна БД PostgreSQL + pgvector на машине; DSN берётся из TEST_DSN в .env
# (дефолт — заглушка, без TEST_DSN интеграционные тесты не подключатся)
python -m pytest
```

- unit: дедупликация (моки LLM/эмбеддингов), парсинг RSS (фикстуры в `tests/fixtures/rss`),
  генерация поста, санитайзер HTML, правила публикации (тихие часы/rate-limit).
- интеграция: полный пайплайн на моках внешних сервисов (LLM/Tavily/Telegram — фейки, БД — реальный Postgres).

## Структура

```
main.py                 точка входа демона (python main.py)
app/
  config.py             pydantic-settings (YAML + env)
  logging.py            JSON-логи, news_id сквозь пайплайн
  db/                   entities (dataclasses), пул asyncpg, репозиторий (raw SQL), schema.sql
  providers/            LLMProvider, EmbeddingProvider, retry
  rss/                  парсинг фидов + poller
  fetcher.py            trafilatura
  pipeline/processor.py очередь + воркеры + статусы
  dedup.py              векторный поиск + LLM-вердикт
  context_search.py     поиск прошлых постов канала
  generator.py          генерация текста поста
  photo/                фото-агент (tavily, vision), downloader
  publish/              планировщик публикаций, sender, сервис модерации
  bot/                  /admin, модерация, статистика
prompts/style.md        редактируемый стайл-гайд канала
emulator/               эмулятор новостей (python emulator/main.py)
tests/                  unit + integration
```
