# news-pilot

Агент ведения новостного Telegram-канала: демон опрашивает RSS-ленты, догружает полные тексты статей,
отсеивает дубликаты (pgvector + LLM), подбирает фото через ИИ-агента с инструментами, генерирует пост
в стиле канала (со ссылками на прошлые посты при развитии истории) и публикует его в Telegram.

HTTP-сервера в приложении нет: источник — RSS, мониторинг — логи и админ-меню бота.

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

## Конфигурация

- `config.yaml` — все параметры (см. `config.example.yaml`): llm, embeddings, rss, fetcher,
  database, tavily, telegram, dedup, photo_agent, context, publish.
- `.env` — секреты, переопределяют YAML: `LLM__API_KEY`, `TAVILY__API_KEY`,
  `TELEGRAM__BOT_TOKEN`, `DATABASE__DSN` (вложенность — через `__`).
- Смена `publish.mode` (`auto` | `moderation`) — только конфиг + рестарт, код менять не нужно.
- `rss.clear_run: true` — при старте все записи, уже лежащие в лентах, сохраняются со статусом
  `cleared` и не обрабатываются; публикуются только новости, появившиеся в лентах после запуска
  (защита от флуда старыми новостями после простоя).
- `publish.append_source: true` — в конец поста в канале добавляется ссылка «Источник» на исходную новость.
- `publish.notify_admin: true` (режим `auto`) — после каждой публикации админ получает уведомление:
  ссылка на пост, источник (кликабельная ссылка на новость) и возраст новости.
- `embeddings.dimensions` (1024) зашита в схему БД; при смене — пересоздать базу (данные не критичны).

## Пайплайн

```
RSS feeds → Poller → [rss.clear_run: при старте всё, что уже в лентах, → статус cleared, конец]
  → догрузка полного текста (trafilatura, fallback на summary)
  → статус pending → эмбеддинг (Ollama)
  → [1] Дедупликация: pgvector top-5 за window_days с порогом min_similarity + LLM-вердикт
      → duplicate / needs_review (on_error: review|pass|drop) — конец
  → [2] Фото-агент: tavily_image_search / inspect_image (vision) / select_images,
      лимиты max_iterations/max_searches/max_images; 0 фото — валидный исход
  → [3] Поиск релевантных опубликованных постов (top-3, context.window_days)
  → [4] Генерация поста по prompts/style.md (structured output, ≤1000 символов, HTML)
  → [5] Публикация: auto (сразу в очередь; append_source — ссылка «Источник» в конце поста;
        notify_admin — уведомление админу о публикации)
        или moderation (черновик админу: Опубликовать / Редактировать / Отклонить, таймаут 24ч)
  → пост + эмбеддинг сохраняются в БД постов
```

Очередь — `asyncio.Queue` между poller и pipeline; новости обрабатываются последовательно,
по одной за раз. Сбой одного этапа помечает новость
`status=failed` и не роняет очередь. Сбой фото/Tavily не блокирует публикацию текстового поста.

## Статусы новости

`pending → dedup → (duplicate | needs_review | photo_search) → writing → (moderation | published)`,
`rejected` — отклонение на модерации или таймаут, `failed` — при ошибках этапа или коротком тексте,
`cleared` — запись была в ленте на момент запуска с `rss.clear_run` (сохранена только как маркер
«уже видел», в пайплайн и поиск дедупа/референсов не попадает).

Черновики постов хранятся в таблице `posts` со статусом `draft` (модерация) / `queued`
(очередь auto) и переотправляются при рестарте; в поиске связанных постов участвуют только
`status=published`. Отклонённые посты (вручную или по таймауту) удаляются из `posts`;
новость получает `status=rejected`.

## Бот

- Один админ: `telegram.admin_id`. Апдейты от остальных игнорируются.
- `/admin` — инлайн-меню: «📊 Статистика», «🔄 Обновить» (статистика считается SQL-запросами на лету,
  включая счётчик пропущенных при clear-запуске).
- Модерация: черновик приходит в личку админу; кнопки «Опубликовать», «Без фото» (если есть фото),
  «Редактировать» (прислать новый текст), «Отклонить». Таймаут (`publish.moderation_timeout_hours`,
  24ч) → отклонение.
- В режиме `auto` с `publish.notify_admin: true` после каждой публикации админу приходит уведомление:
  ссылка на пост, источник и возраст новости.

## Тесты

```bash
# нужна БД PostgreSQL + pgvector на машине; DSN берётся из TEST_DSN в .env
# (дефолт — заглушка, без TEST_DSN интеграционные тесты не подключатся)
python -m pytest
```

- unit: дедупликация (моки LLM/эмбеддингов), парсинг RSS (фикстуры в `tests/fixtures/rss`),
  генерация поста, санитайзер HTML, фото-агент.
- интеграция: полный пайплайн на моках внешних сервисов (LLM/Tavily/Telegram — фейки, БД — реальный Postgres),
  clear-запуск poller (статус `cleared`, изоляция от дедупа/референсов), модерация, уведомления админу,
  добавление источника к посту.

## Структура

```
main.py                 точка входа демона (python main.py)
app/
  config.py             pydantic-settings (YAML + env)
  logging.py            human-readable однострочные логи, news_id сквозь пайплайн
  db/                   entities (dataclasses), пул asyncpg, репозиторий (raw SQL), schema.sql
  providers/            LLMProvider, EmbeddingProvider, retry
  rss/                  парсинг фидов + poller
  fetcher.py            trafilatura
  pipeline/processor.py очередь + последовательная обработка + статусы
  dedup.py              векторный поиск + LLM-вердикт
  context_search.py     поиск прошлых постов канала
  generator.py          генерация текста поста
  photo/                фото-агент (tavily, vision), downloader
  publish/              планировщик публикаций, sender, сервис модерации
  bot/                  /admin, модерация, статистика
prompts/style.md        редактируемый стайл-гайд канала
tests/                  unit + integration
```
