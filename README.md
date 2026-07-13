# Web Chat · Local LLM

Веб-чат поверх **локальной** LLM через [Ollama](https://ollama.com) (`qwen2.5-coder:3b`,
оптимизирована под задачу) с **RAG** поверх локального индекса: retrieval и генерация
полностью на VPS, ответы с дословными цитатами и гейтом «не знаю». Тот же интерфейс, что у
соседнего `../webchat`, но вместо облачного DeepSeek — модель, крутящаяся прямо на сервере.

- 🌐 **Живой чат:** https://llm.jorchik.com
- 📊 **Сравнение local vs cloud** (качество / скорость / стабильность): [`eval/REPORT-local-vs-cloud.md`](eval/REPORT-local-vs-cloud.md)
- ⚙️ **Оптимизация под задачу** (до/после: параметры, промпт, модель): [`eval/REPORT-optimization.md`](eval/REPORT-optimization.md)

## Архитектура

```
Браузер ──HTTPS──▶ nginx (llm.jorchik.com:443)
                      ├─ /       → статика фронта  /var/www/webchat-local
                      └─ /chat   → backend  127.0.0.1:8010  (systemd: webchat-local.service)
                                       │
                                       └─ Ollama  127.0.0.1:11434  (qwen2.5:3b, OpenAI-совместимый API)
```

- Браузер **не** ходит в Ollama напрямую — только через свой backend (единый origin).
- Retrieval — по HTTP к соседнему `rag`-сервису (`:8100`), генерация — Ollama (`:11434`).
- Контракт: `POST /chat {messages, useRag, improvedRag, temperature} → {reply, sources, rewrittenQuery, ragMeta}`, `GET /health → {ok}`.

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
`OLLAMA_URL` (по умолч. нативный `http://127.0.0.1:11434/api/chat`), `RAG_URL`
(по умолч. `http://127.0.0.1:8100`). RAG-режиму нужен запущенный `rag`-сервис.

## Деплой
См. [`deploy/README.md`](deploy/README.md) — systemd + nginx на поддомене
`llm.jorchik.com` (нужна DNS A-запись + `certbot --nginx -d llm.jorchik.com`).

## Замечание про скорость
`qwen2.5:3b` работает на CPU — grounded-ответ с RAG занимает **~60–250 c** (против ~8 c у
облачного DeepSeek, см. отчёт сравнения). httpx-таймаут backend — `OLLAMA_TIMEOUT` (по умолч.
300 c). На 3.8 ГБ RAM нужен swap: qwen @ num_ctx=4096 иначе упирается в OOM-killer.
