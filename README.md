# news-pilot

Агент ведения новостного Telegram-канала: демон опрашивает RSS-ленты, догружает полные тексты статей,
отсеивает дубликаты (pgvector + LLM), подбирает фото через ИИ-агента с инструментами, генерирует пост
в стиле канала (со ссылками на прошлые посты при развитии истории) и публикует его в Telegram.

HTTP-сервера в приложении нет: источник — RSS, мониторинг — логи (JSON) и админ-меню бота.

## Стек

Python 3.12, feedparser, trafilatura, PostgreSQL 16 + pgvector, asyncpg (raw SQL),
OpenAI-compatible LLM (openai SDK), Ollama (эмбеддинги), tavily-python, aiogram 3, pydantic-settings.

## Запуск

### Локально (для разработки)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env               # заполнить секреты
cp config.example.yaml config.yaml # настроить фиды/канал/режим

# БД (PostgreSQL 16 + pgvector)
docker run -d --name news-pilot-pg -e POSTGRES_USER=news -e POSTGRES_PASSWORD=news \
  -e POSTGRES_DB=news -p 5432:5432 pgvector/pgvector:pg16

# Ollama (эмбеддинги)
ollama pull nomic-embed-text

.venv/bin/python main.py
```

Схема БД создаётся автоматически при старте (`app/db/schema.sql`, только `CREATE ... IF NOT EXISTS`).

### Docker

```bash
cp .env.example .env && cp config.example.yaml config.yaml  # заполнить
docker compose up -d --build          # app + postgres
docker compose --profile optional up -d ollama   # если нужен локальный Ollama
```

Фид эмулятора в конфиге приложения: `http://host.docker.internal:PORT/rss`
(в compose добавлен `host-gateway`).

## Конфигурация

- `config.yaml` — все параметры (см. `config.example.yaml`): llm, embeddings, rss, fetcher,
  database, tavily, telegram, dedup, photo_agent, context, publish, pipeline.
- `.env` — секреты, переопределяют YAML: `LLM__API_KEY`, `TAVILY__API_KEY`,
  `TELEGRAM__BOT_TOKEN`, `DATABASE__DSN` (вложенность — через `__`).
- Смена `publish.mode` (`auto` | `moderation`) — только конфиг + рестарт, код менять не нужно.
- `embeddings.dimensions` (768) зашита в схему БД; при смене — пересоздать базу (данные не критичны).

## Пайплайн

```
RSS feeds → Poller → догрузка полного текста (trafilatura, fallback на summary)
  → статус pending → эмбеддинг (Ollama)
  → [1] Дедупликация: pgvector top-5 за window_days с порогом min_similarity + LLM-вердикт
      → duplicate / needs_review (on_error: review|pass|drop) — конец
  → [2] Фото-агент: enclosures → tavily_image_search / inspect_image (vision) / select_images,
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
`failed` — при ошибках этапа или коротком тексте.

Черновики постов хранятся в таблице `posts` со статусом `draft` (модерация) / `queued`
(очередь auto) и переотправляются при рестарте; в поиске связанных постов участвуют только
`status=published`. Отклонённые посты получают `status=rejected` (для статистики).

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
# нужна БД PostgreSQL + pgvector (порт 5432 или свой DSN в tests/conftest.py)
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
