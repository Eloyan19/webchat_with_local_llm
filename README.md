# Web Chat · Local LLM

Минимальный веб-чат поверх **локальной** LLM через [Ollama](https://ollama.com)
(`qwen2.5:3b`). Тот же интерфейс, что у соседнего `../webchat`, но вместо облачного
DeepSeek — модель, крутящаяся прямо на VPS. Без RAG, цитат и «памяти задачи» —
намеренно минимально.

Публичный URL (прод): **https://llm.jorchik.com**

## Архитектура

```
Браузер ──HTTPS──▶ nginx (llm.jorchik.com:443)
                      ├─ /       → статика фронта  /var/www/webchat-local
                      └─ /chat   → backend  127.0.0.1:8010  (systemd: webchat-local.service)
                                       │
                                       └─ Ollama  127.0.0.1:11434  (qwen2.5:3b, OpenAI-совместимый API)
```

- Браузер **не** ходит в Ollama напрямую — только через свой backend (единый origin).
- Контракт: `POST /chat {messages:[{role,content}]} → {reply}`, `GET /health → {ok}`.

## Стек
TypeScript · React · Vite · Python · FastAPI · httpx · localStorage.

## Локальная разработка

```sh
# backend
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8010     # нужен запущенный Ollama на :11434

# frontend
cd frontend
npm install
npm run dev                                # Vite dev-server, зовёт http://localhost:8010/chat
```

Модель и адрес Ollama настраиваются через env: `OLLAMA_MODEL` (по умолч. `qwen2.5:3b`),
`OLLAMA_URL` (по умолч. `http://127.0.0.1:11434/v1/chat/completions`).

## Деплой
См. [`deploy/README.md`](deploy/README.md) — systemd + nginx на поддомене
`llm.jorchik.com` (нужна DNS A-запись + `certbot --nginx -d llm.jorchik.com`).

## Замечание про скорость
`qwen2.5:3b` работает на CPU — ответ приходит **заметно медленнее**, чем от облачного
DeepSeek. Таймауты (httpx 180 c, nginx `proxy_read_timeout 180s`) выставлены с запасом.
