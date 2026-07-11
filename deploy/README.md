# Деплой webchat-local (локальная LLM, Ollama) на VPS (jorchik.com)

Публичный URL: **https://llm.jorchik.com** (отдельный поддомен, TLS — Certbot).

## Компоненты
- **backend** — `webchat-local.service` (systemd), uvicorn на `127.0.0.1:8010`.
  Проксирует `/chat` в локальный Ollama (`OLLAMA_URL`/`OLLAMA_MODEL` из
  `backend/.env`, в git не попадает). Без внешних ключей — модель локальная.
- **Ollama** — `qwen2.5:3b`, слушает `127.0.0.1:11434` (общий для всех проектов
  на VPS, отдельно не поднимаем; юнит уже существует как `ollama.service`).
- **frontend** — прод-сборка (`npm run build`), выложена в
  `/var/www/webchat-local`, раздаётся nginx.
- **nginx** — отдельный `server{}` блок из `nginx-llm.conf` для
  `server_name llm.jorchik.com`: `/` → статика, `/chat` (с gate-токеном) и
  `/health` → проксируются на backend.

## Первичная установка

```sh
# 0. DNS: добавить A-запись llm.jorchik.com -> IP этого VPS, дождаться пропагации

# 1. backend service
sudo cp deploy/webchat-local.service /etc/systemd/system/webchat-local.service
sudo systemctl daemon-reload
sudo systemctl enable --now webchat-local.service

# 2. frontend static
cd frontend && npm run build && cd ..
sudo mkdir -p /var/www/webchat-local
sudo cp -r frontend/dist/. /var/www/webchat-local/

# 3. nginx: rate-limit зона (http-контекст)
sudo cp deploy/nginx-ratelimit.conf /etc/nginx/conf.d/webchat-local-ratelimit.conf

# 4. nginx: отдельный server-блок под поддомен.
#    В nginx-llm.conf токен — плейсхолдер __CHAT_TOKEN__ (в git реальный не храним).
#    Подставь тот же токен, что в frontend/.env.production (VITE_CHAT_TOKEN):
TOKEN=<тот-же-токен-что-в-frontend/.env.production>
sudo sed "s/__CHAT_TOKEN__/$TOKEN/g" deploy/nginx-llm.conf \
  > /etc/nginx/sites-available/llm.jorchik.com
sudo ln -s /etc/nginx/sites-available/llm.jorchik.com /etc/nginx/sites-enabled/llm.jorchik.com
sudo nginx -t && sudo systemctl reload nginx

# 5. TLS
sudo certbot --nginx -d llm.jorchik.com
```

## Обновление (redeploy)

```sh
# backend
sudo systemctl restart webchat-local.service

# frontend
cd frontend && npm run build && cd ..
sudo rm -rf /var/www/webchat-local/*
sudo cp -r frontend/dist/. /var/www/webchat-local/
```

## Проверка

```sh
curl https://llm.jorchik.com/health

# Bearer — тот же токен, что вшит во фронт (VITE_CHAT_TOKEN). Он НЕ секрет:
# Vite вшивает его в публичный JS-бандл, любой может достать из браузера.
# Защита от нагрузки — limit_req (зона chat_local), а не этот токен.
curl -X POST https://llm.jorchik.com/chat \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <VITE_CHAT_TOKEN>' \
  -d '{"messages":[{"role":"user","content":"ping"}]}'
```

Ответ от 3B-модели на CPU может занимать десятки секунд (иногда больше минуты
на длинных промптах) — это ожидаемо, не баг. `proxy_read_timeout 180s` в nginx
и `httpx.AsyncClient(timeout=180)` в backend подобраны под это.
