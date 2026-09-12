# news-pilot

Агент ведения новостного Telegram-канала: демон опрашивает RSS-ленты, догружает полные тексты статей,
отсеивает дубликаты (pgvector + LLM), подбирает фото через ИИ-агента с инструментами, генерирует пост
в стиле канала (со ссылками на прошлые посты при развитии истории) и публикует его в Telegram.

HTTP-сервера в приложении нет: источник — RSS, мониторинг — логи и админ-меню бота.

## Возможности

- Опрос произвольного числа RSS-лент по расписанию (`rss.poll_interval_seconds`).
- Догрузка полного текста статьи по ссылке (trafilatura, fallback на summary из ленты).
- Дедупликация: векторный поиск ближайших новостей (pgvector) + LLM-вердикт.
- Подбор фото: Tavily-поиск картинок + vision-оценка кандидатов LLM (0–4 фото на пост).
- Поиск связанных прошлых постов канала («как мы писали ранее») по эмбеддингам.
- Генерация поста в стиле канала (HTML, ≤1000 символов) — стиль задаётся в `prompts/style.md`.
- Публикация: сразу в канал (`auto`) или через модерацию админом (`moderation`).
- Админ-бот: инлайн-статистика, модерация черновиков, уведомления о публикациях.
- Независимые прокси (socks5/http) для каждого компонента: rss, llm, embeddings, photos, publish, telegram.
- Восстановление после рестарта: незавершённые новости переобрабатываются, черновики/очередь — переотправляются.

## Стек

Python 3.12, feedparser, trafilatura, PostgreSQL 16+ с pgvector (на хост-машине), asyncpg (raw SQL),
OpenAI-compatible LLM (openai SDK), Ollama (эмбеддинги), tavily-python, aiogram 3, pydantic-settings.

## Предварительные требования

| Компонент | Назначение | Примечание |
|---|---|---|
| Python 3.12 | локальный запуск / сборка Docker-образа | в контейнере не нужен |
| PostgreSQL 16+ с pgvector | хранение новостей, постов, эмбеддингов | на хост-машине, вне Docker |
| Ollama | эмбеддинги (дедуп + поиск связанных постов) | локально или в compose (`--profile optional`) |
| Telegram-бот | публикация в канал + админ-меню | @BotFather |
| Ключ OpenAI-compatible LLM | генерация, дедуп-вердикт, фото-агент | OpenAI / OpenRouter / любой совместимый шлюз |
| Ключ Tavily | поиск фото-кандидатов | бесплатный: https://tavily.com |

## 1. Установка

### Вариант A — локально (для разработки)

```bash
# 1) Виртуальное окружение и зависимости
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) Файлы конфигурации
cp .env.example .env               # заполнить секреты (см. раздел «Конфигурация»)
cp config.example.yaml config.yaml # настроить фиды/канал/режим
```

#### PostgreSQL + pgvector (Debian/Ubuntu)

```bash
sudo apt install postgresql
# пакет pgvector: имя зависит от версии PG, например postgresql-16-pgvector
sudo apt install postgresql-16-pgvector

# роль и база — любые; всё это прописывается в .env (DATABASE__DSN)
sudo -u postgres psql -c "CREATE ROLE мой_юзер LOGIN PASSWORD 'мой_пароль';"
sudo -u postgres createdb -O мой_юзер моя_база
```

Расширение `vector` включается автоматически при первом старте приложения
(`app/db/schema.sql` выполняет `CREATE EXTENSION IF NOT EXISTS vector;`).
Схема БД тоже создаётся автоматически (`CREATE ... IF NOT EXISTS`).

В `.env` указать (юзер/пароль/база — ваши):

```
DATABASE__DSN=postgresql+asyncpg://мой_юзер:мой_пароль@localhost:5432/моя_база
TEST_DSN=postgresql+asyncpg://мой_юзер:мой_пароль@localhost:5432/моя_база_test
```

#### Ollama (эмбеддинги)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3-embedding:0.6b-q4_K_M   # модель по умолчанию: 1024 dims, ~1 ГБ RAM
```

#### Запуск

```bash
.venv/bin/python main.py
```

### Вариант B — Docker

PostgreSQL в compose **нет** — требуется работающий PostgreSQL на хост-машине
(прослушивает localhost, юзер/база — из `DATABASE__DSN`).

```bash
cp .env.example .env && cp config.example.yaml config.yaml  # заполнить
docker compose up -d --build                  # app (БД — на хосте)
docker compose --profile optional up -d ollama  # если нужен локальный Ollama (том ollama персистентен)
```

Из контейнера БД доступна по `host.docker.internal:5432` (в compose добавлен `host-gateway`),
поэтому в `.env`: `DATABASE__DSN=postgresql+asyncpg://юзер:пароль@host.docker.internal:5432/база`.

PostgreSQL должен слушать TCP (в `postgresql.conf` `listen_addresses = 'localhost'` — достаточно).
Для доступа из контейнера на Linux добавьте правило в `pg_hba.conf` для docker-моста,
например: `host all all 172.16.0.0/12 scram-sha-256`, затем `sudo systemctl reload postgresql`.

В контейнере монтируются только `app/`, `prompts/`, `main.py` (образ) и `config.yaml:ro` (том);
`.env` передаётся через `env_file`. Файлы на диск не пишутся (фото живут в памяти).

### Вариант C — systemd (прод)

После локальной установки (`Вариант A`):

```ini
# /etc/systemd/system/news-pilot.service
[Unit]
Description=news-pilot
After=network.target postgresql.service

[Service]
User=nik
WorkingDirectory=/home/nik/PycharmProjects/news-pilot
ExecStart=/home/nik/PycharmProjects/news-pilot/.venv/bin/python main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now news-pilot
journalctl -u news-pilot -f    # логи
```

## 2. Конфигурация

### Источники и приоритет

Параметры складываются в объект `Settings` (`app/config.py`) из четырёх источников
(выше в списке = выше приоритет):

1. **Переменные окружения** (реальный `export VAR=...`);
2. **`.env`** (путь переопределяется переменной `ENV_FILE`);
3. **YAML-файл** (путь: переменная `CONFIG`, иначе строка `CONFIG=...` в `.env`, иначе `config.yaml` в корне);
4. **Значения по умолчанию** из кода (`app/config.py`).

Вложенность YAML в env — через `__`: `llm.temperature` → `LLM__TEMPERATURE`,
`telegram.bot_token` → `TELEGRAM__BOT_TOKEN` и т.д. **Любой** параметр YAML можно переопределить
переменной окружения. Неизвестные ключи в YAML/.env запрещены (`extra=forbid`) — опечатка упадёт с явной ошибкой.

### `.env` — секреты и точки подключения

| Переменная | Описание | Пример |
|---|---|---|
| `CONFIG` | путь к YAML-конфигу | `config.yaml` |
| `ENV_FILE` | путь к самому `.env` (нестандартное расположение) | `secrets/prod.env` |
| `DATABASE__DSN` | DSN приложения; схема создаётся при старте | `postgresql+asyncpg://user:pass@localhost:5432/news` |
| `TEST_DSN` | DSN для интеграционных тестов pytest (не обязателен, если тесты не запускаете) | `postgresql+asyncpg://user:pass@localhost:5432/news_test` |
| `LLM__API_KEY` | API-ключ OpenAI-compatible шлюза | `sk-...` |
| `TAVILY__API_KEY` | ключ поиска фото | `tvly-...` |
| `TELEGRAM__BOT_TOKEN` | токен бота (@BotFather → /newbot) | `123456:ABC...` |

Полный список: `.env.example`. Секреты намеренно не хранятся в `config.yaml` (он может лежать в git).

### `config.yaml` — все параметры

Полный прокомментированный шаблон: `config.example.yaml`. Значения по умолчанию — из `app/config.py`.

| Секция | Поле | По умолчанию | Описание |
|---|---|---|---|
| `log_level` | | `INFO` | `DEBUG \| INFO \| WARNING \| ERROR` |
| `llm` | `base_url` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| | `model` | `gpt-4o-mini` | должна поддерживать vision и tool-calling |
| | `temperature` | `0.4` | 0 = строго, 1 = свободно |
| | `timeout_seconds` | `120` | таймаут запроса |
| | `retries` | `3` | ретраи с backoff |
| | `proxy` | *(пусто)* | `socks5://user:pass@host:1080` или `http://...` |
| | `extra_headers` | `{}` | доп. заголовки (шлюзы с сессионной авторизацией) |
| `embeddings` | `base_url` | `http://localhost:11434` | Ollama |
| | `model` | `qwen3-embedding:0.6b-q4_K_M` | |
| | `dimensions` | `1024` | зашита в схему БД; смена = пересоздать базу |
| | `max_chars` | `6000` | сколько символов текста подавать на эмбеддинг |
| | `proxy` | *(пусто)* | |
| `rss` | `poll_interval_seconds` | `300` | интервал опроса всех фидов |
| | `clear_run` | `false` | см. «Clear-запуск» ниже |
| | `feeds` | `[]` | список `{name, url}`; name виден в админке |
| `fetcher` | `timeout_seconds` | `30` | догрузка статей и опрос лент |
| | `retries` | `2` | |
| | `min_text_length` | `100` | короче — новость получает `failed` |
| | `proxy` | *(пусто)* | |
| `tavily` | `timeout_seconds` | `30` | поиск фото-кандидатов |
| | `retries` | `3` | |
| `telegram` | `channel_id` | `@channel` | `@username` публичного канала или числовой id `-100...` |
| | `admin_id` | `0` | Telegram user id админа (модерация, /admin) |
| | `proxy` | *(пусто)* | |
| `dedup` | `window_days` | `3` | искать дубли среди новостей за N дней |
| | `min_similarity` | `0.75` | порог косинусной близости кандидатов |
| | `top_k` | `5` | сколько ближайших показывать LLM |
| | `on_error` | `review` | при ошибке LLM: `review` → needs_review, `pass` → уникальная, `drop` → отбросить |
| `photo_agent` | `max_iterations` | `10` | максимум шагов tool-calling-цикла |
| | `max_searches` | `5` | максимум tavily-запросов на новость |
| | `max_images` | `4` | максимум фото в посте (1–4) |
| | `proxy` | *(пусто)* | скачивание фото |
| `context` | `window_days` | `14` | искать прошлые посты за N дней |
| | `top_k` | `3` | сколько постов подавать в контекст |
| | `min_similarity` | `0.5` | 0.7 режет ~60% релевантного |
| `publish` | `mode` | `moderation` | `auto` — сразу в канал; `moderation` — черновик админу |
| | `moderation_timeout_hours` | `24` | неотвеченный черновик отклоняется |
| | `notify_admin` | `false` | в режиме auto: уведомление админу о каждой публикации |
| | `append_source` | `false` | добавлять в конец поста ссылку «Источник» |
| | `proxy` | *(пусто)* | докачка фото при повторной публикации |

Прокси (`socks5://` / `http://`) поддерживаются отдельно для rss/fetcher, llm, embeddings,
photo_agent, publish и telegram — удобно, когда разные сервисы доступны по разным маршрутам.

### Telegram: подготовка

1. **Бот**: @BotFather → `/newbot` → токен в `TELEGRAM__BOT_TOKEN`.
2. **Канал**: добавить бота **администратором** канала с правом «Публикация сообщений»;
   `telegram.channel_id` — `@username` публичного канала или числовой id (`-100...`).
3. **admin_id**: свой Telegram user id (узнать можно у @userinfobot) → `telegram.admin_id`.
   Апдейты от остальных пользователей игнорируются.

### Режимы публикации

- `publish.mode: auto` — пост уходит в канал сразу; с `notify_admin: true` админ получает
  уведомление (ссылка на пост, источник, возраст новости).
- `publish.mode: moderation` — черновик приходит админу в личку: «Опубликовать», «Без фото»
  (если фото есть), «Редактировать» (прислать новый текст), «Отклонить». Неотвеченный черновик
  через `moderation_timeout_hours` отклоняется автоматически.
- Смена режима — только правка конфига + рестарт, код менять не нужно.

### Clear-запуск

`rss.clear_run: true` — при старте все записи, уже лежащие в лентах, сохраняются со статусом
`cleared` и не обрабатываются; публикуются только новости, появившиеся в лентах **после** запуска.
Защита от флуда старыми новостями после простоя. Счётчик пропущенных виден в `/admin`.

### Стиль канала

`prompts/style.md` — редактируемый стайл-гайд: правила заголовков, тон, эмодзи, формат ссылок
на прошлые посты. Используется при генерации каждого поста.

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
        или moderation (черновик админу, таймаут 24ч)
  → пост + эмбеддинг сохраняются в БД постов
```

Очередь — `asyncio.Queue` между poller и pipeline; новости обрабатываются последовательно,
по одной за раз. Сбой одного этапа помечает новость `status=failed` и не роняет очередь.
Сбой фото/Tavily не блокирует публикацию текстового поста.

## Статусы новости

`pending → dedup → (duplicate | needs_review | photo_search) → writing → (moderation | published)`,
`rejected` — отклонение на модерации или таймаут, `failed` — при ошибках этапа или коротком тексте,
`cleared` — запись была в ленте на момент запуска с `rss.clear_run` (сохранена только как маркер
«уже видел», в пайплайн и поиск дедупа/референсов не попадает).

Черновики постов хранятся в таблице `posts` со статусом `draft` (модерация) / `queued`
(очередь auto) и переотправляются при рестарте; в поиске связанных постов участвуют только
`status=published`. Отклонённые посты (вручную или по таймауту) удаляются из `posts`;
новость получает `status=rejected`.

Фото нигде не persist'ятся: байты скачанных картинок живут только в памяти
(от выбора фото агентом до публикации/отклонения), в БД сохраняются лишь `source_url`.
После рестарта фото queued/draft постов перекачиваются по URL; если не удалось —
пост публикуется без фото. На диск ничего не пишется (том `images` в compose не нужен).

## Бот

- Один админ: `telegram.admin_id`. Апдейты от остальных игнорируются.
- `/admin` — инлайн-меню: «📊 Статистика», «🔄 Обновить» (статистика считается SQL-запросами
  на лету, включая счётчик пропущенных при clear-запуске).
- Модерация: черновик приходит в личку админу; кнопки «Опубликовать», «Без фото» (если есть фото),
  «Редактировать» (прислать новый текст), «Отклонить». Таймаут (`publish.moderation_timeout_hours`,
  24ч) → отклонение.
- В режиме `auto` с `publish.notify_admin: true` после каждой публикации админу приходит
  уведомление: ссылка на пост, источник и возраст новости.

## Тесты

```bash
# Для интеграционных тестов нужна реальная БД PostgreSQL + pgvector;
# DSN берётся из TEST_DSN (.env или переменная окружения). Без TEST_DSN
# интеграционные тесты не подключатся; юнит-тесты работают без БД.
.venv/bin/python -m pytest
```

- unit: дедупликация (моки LLM/эмбеддингов), парсинг RSS (фикстуры в `tests/fixtures/rss`),
  генерация поста, санитайзер HTML, фото-агент.
- интеграция: полный пайплайн на моках внешних сервисов (LLM/Tavily/Telegram — фейки,
  БД — реальный Postgres), clear-запуск poller (статус `cleared`, изоляция от дедупа/референсов),
  модерация, уведомления админу, добавление источника к посту.

## Структура

```
main.py                 точка входа демона (python main.py)
app/
  config.py             pydantic-settings (YAML + env, приоритет env > .env > yaml)
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

## Устранение неполадок

| Симптом | Причина / решение |
|---|---|
| `extension "vector" is not available` | не установлен пакет pgvector (`postgresql-16-pgvector` и т.п.), переустановить и пересоздать БД |
| Ошибка подключения к БД | проверить `DATABASE__DSN`; из Docker — `host.docker.internal` вместо `localhost` и правило в `pg_hba.conf` |
| Размерность вектора не совпадает | `embeddings.dimensions` зашита в схему; вернуть 1024 или пересоздать базу |
| `telegram.bot_token is missing` | не заполнен `TELEGRAM__BOT_TOKEN` в `.env` |
| Посты не приходят в канал | бот не админ канала или нет права публикации; проверить `channel_id` |
| `cleared` сразу после старта | это `rss.clear_run: true` — старые записи пропускаются сознательно |
| 403 при скачивании фото | часть CDN требует браузерные заголовки (уже зашиты в `main.py`); поможет `photo_agent.proxy` |
| Эмбеддинги падают | Ollama не запущена или модель не скачана (`ollama pull qwen3-embedding:0.6b-q4_K_M`) |
