# Деплой news-pilot на Windows-сервер: WSL2 + Docker Engine

Схема: внутри WSL2 VM (Ubuntu, systemd) работают Docker Engine и PostgreSQL 16 + pgvector
(нативный пакет, не контейнер). Контейнер news-pilot ходит на БД через
`host.docker.internal` (= docker-мост WSL VM).

Почему так: Docker Desktop живёт в пользовательской сессии и глохнет при logoff из RDP;
Docker Engine внутри WSL2 с systemd переживает и logoff, и ребут (VM поднимается
задачей планировщика, systemd держит её живой, dockerd восстанавливает контейнеры).

## 0. Требования

| Требование | Как проверить |
|---|---|
| Windows Server 2022/2025 (на 2019 WSL2 нет) | `winver` |
| Виртуализация включена в BIOS | `systeminfo` → «Virtualization Enabled In Firmware: Yes» |
| Если сервер — виртуалка: nested virtualization | Hyper-V: `Set-VMProcessor -ExposeVirtualizationExtensions $true`; ESXi: «Expose hardware assisted virtualization» |
| Исходящий интернет | api.telegram.org, api.tavily.com, LLM-эндпоинт, RSS-хосты, Docker Hub, apt |
| 4+ ГБ RAM свободно под WSL VM | PG + app + Ollama (~1 ГБ на модель) |

## 1. Windows: включить WSL2

PowerShell от администратора:

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
Restart-Computer
```

После ребута — современный WSL (Store на Server нет): скачать `wsl.2.x.x.x64.msi`
с https://github.com/microsoft/WSL/releases, установить, проверить `wsl --version` (2.x).

```powershell
wsl --set-default-version 2
wsl --install -d Ubuntu --no-launch
```

Fallback: скачать `Ubuntu2404-*.wsl` с https://ubuntu.com/desktop/wsl и
`wsl --install --from-file .\Ubuntu2404-*.wsl`.

Лимиты VM — `C:\Users\<юзер>\.wslconfig`:

```ini
[wsl2]
memory=6GB
processors=4
```

## 2. WSL: systemd

В `wsl -d Ubuntu`:

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

Из PowerShell: `wsl --shutdown`, затем снова `wsl -d Ubuntu`.
Проверка: `systemctl` выдаёт список юнитов.

## 3. Docker Engine внутри Ubuntu

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
docker run --rm hello-world
```

## 4. PostgreSQL 16 + pgvector внутри Ubuntu

```bash
sudo apt-get install -y postgresql-16 postgresql-16-pgvector
sudo -u postgres psql -c "CREATE ROLE news LOGIN PASSWORD 'СЛОЖНЫЙ_ПАРОЛЬ';"
sudo -u postgres createdb -O news news
sudo -u postgres psql -d news -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

`/etc/postgresql/16/main/postgresql.conf`:

```conf
listen_addresses = '*'    # VM за NAT винды, наружу недоступна
```

`/etc/postgresql/16/main/pg_hba.conf` (контейнеры приходят с docker-моста):

```conf
host  news  news  172.17.0.0/16  scram-sha-256
```

```bash
sudo systemctl restart postgresql
```

## 5. Автостарт WSL (переживает logoff и ребут)

WSL-дистрибутивы регистрируются на пользователя, поэтому задача — от учётки владельца
дистрибутива, с хранимым паролем («выполнять вне зависимости от входа пользователя»):

```powershell
schtasks /Create /TN "WSL news-pilot boot" `
  /TR "C:\Windows\System32\wsl.exe -d Ubuntu -e /bin/true" `
  /SC ONSTART /DELAY 0001:00 /RL HIGHEST `
  /RU ВАШ_ЮЗЕР /RP ВАШ_ПАРОЛЬ /F
```

Цепочка при загрузке: планировщик → `wsl.exe` → WSL VM + дистрибутив → systemd (PID 1,
не завершается, VM не гаснет) → `docker.service` + `postgresql` → dockerd восстанавливает
контейнеры (`restart: unless-stopped`). Если политика запрещает хранить пароль —
автологон сервисной учётки (Sysinternals Autologon).

## 6. Деплой проекта

Проект держать в Linux-ФС (`/opt/news-pilot`), не на `/mnt/c/...` — bind-монты с NTFS
медленные и ломают сборку.

```bash
sudo mkdir -p /opt && sudo chown $USER /opt
cd /opt
git clone <URL_РЕПОЗИТОРИЯ> news-pilot      # или скопировать через \\wsl.localhost\Ubuntu\opt\
cd news-pilot
cp .env.example .env && cp config.example.yaml config.yaml
```

`.env` и `config.yaml` — как в `deploy/ubuntu.md`, п. 4 (секция «Деплой проекта»).
Для этой схемы PostgreSQL внутри WSL, поэтому DSN-хост `host.docker.internal`
резолвится в docker-мост (172.17.0.1) — настройки по умолчанию подходят.

Запуск:

```bash
docker compose up -d --build
docker compose --profile optional up -d ollama
docker compose exec ollama ollama pull qwen3-embedding:0.6b
```

## 7. Проверка

1. `docker compose ps` — Up; `docker compose logs -f app` — «news-pilot started».
2. `/admin` у бота отвечает, бот — админ канала.
3. **Тест logoff**: выйти из RDP (logoff), подождать 10–15 мин, зайти → `docker ps`:
   uptime контейнера больше времени отсутствия (не перезапускался), новые посты идут.
4. **Тест ребута**: перезагрузить сервер → не логинясь, через 3–5 мин проверить
   бота удалённо (`/admin`).

## 8. Эксплуатация

```bash
docker compose logs -f --since 5m app    # логи
docker compose restart                   # после правки config.yaml
docker compose up -d --build             # после git pull
sudo -u postgres pg_dump news > backup-$(date +%F).sql
```

Данные живут только в PostgreSQL (фото — в памяти); бэкап БД = бэкап всего.

## 9. Траблшутинг

| Симптом | Причина / решение |
|---|---|
| `wsl: Ubuntu не найдена` в задаче планировщика | Задача от SYSTEM/чужого юзера — дистрибутив per-user; запускать от владельца с хранимым паролем |
| VM гаснет через ~1 мин | systemd не включён (`/etc/wsl.conf` → `wsl --shutdown`) |
| `connection refused` к БД | `listen_addresses` не `*` или нет правила pg_hba для `172.17.0.0/16` |
| RBC/некоторые сайты 401 | Внешняя блокировка клиента; источник можно убрать из `rss.feeds` |
| Ollama 400 `llama-server process no longer running` | OOM — см. README «Траблшутинг» (env для ollama уже в compose) |
