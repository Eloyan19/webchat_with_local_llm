# Web Chat · Local LLM

Веб-чат поверх **локальной** LLM через [Ollama](https://ollama.com) (`qwen2.5-coder:3b`,
оптимизирована под задачу) с **RAG** поверх локального индекса: retrieval и генерация
полностью на VPS, ответы с дословными цитатами и гейтом «не знаю». Тот же интерфейс, что у
соседнего `../webchat`, но вместо облачного DeepSeek — модель, крутящаяся прямо на сервере.

- 🌐 **Живой чат:** https://llm.jorchik.com
- 📊 **Сравнение local vs cloud** (качество / скорость / стабильность): [`eval/REPORT-local-vs-cloud.md`](eval/REPORT-local-vs-cloud.md)
- ⚙️ **Оптимизация под задачу** (до/после: параметры, промпт, модель): [`eval/REPORT-optimization.md`](eval/REPORT-optimization.md)
- ❓ **Примеры вопросов** (на что отвечает RAG-режим): [`EXAMPLE-QUESTIONS.md`](EXAMPLE-QUESTIONS.md)

## Архитектура

```
Браузер ──HTTPS──▶ nginx (llm.jorchik.com:443)
                      ├─ /       → статика фронта  /var/www/webchat-local
                      └─ /chat   → backend  127.0.0.1:8010  (systemd: webchat-local.service)
                                       │
                                       └─ Ollama  127.0.0.1:11434  (qwen2.5-coder:3b, нативный /api/chat)
```

- Браузер **не** ходит в Ollama напрямую — только через свой backend (единый origin).
- Retrieval — по HTTP к соседнему `rag`-сервису (`:8100`), генерация — Ollama (`:11434`).

## HTTP API

Приватный AI-сервис поверх локальной LLM. Публичный вход — `https://llm.jorchik.com`,
защищён gate-токеном (заголовок `Authorization: Bearer <token>`) и rate-limit'ом на nginx.

**`GET /health`** → `{"ok": true}` — без токена, для проверки живости.

**`POST /chat`** (нужен `Authorization: Bearer <token>`):
```jsonc
// запрос
{
  "messages":   [{"role": "user", "content": "..."}],  // история диалога
  "useRag":      true,      // RAG-режим (ответ с цитатами из индекса) или обычный чат
  "improvedRag": true,      // query-rewrite + rerank + порог (при useRag)
  "temperature": 0          // 0 = детерминированно/точно, 1 = креативно
}
// ответ
{
  "reply":   "...",                     // текст ответа (или «не знаю», если нет источника)
  "sources": [{"file","section","quote","chunk_id"}],  // цитаты (в RAG-режиме)
  "rewrittenQuery": "...",              // переписанный поисковый запрос (или null)
  "ragMeta": { "abstained": false, ... }// диагностика: порог, отброшенные цитаты и т.п.
}
```
Пример:
```sh
curl -X POST https://llm.jorchik.com/chat \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What fields does the ErrorMessage data class in JetNews contain?"}],"useRag":true,"improvedRag":true,"temperature":0}'
```

## Ограничения и приватность

| | Значение |
|---|---|
| **Авторизация** | gate-токен (`Bearer`), проверяется nginx до backend |
| **Rate limit** | 30 req/min на IP, burst 10 (nginx `limit_req`), сверх → `429` |
| **Max context** | окно модели `num_ctx=3072`; фронт шлёт последние 20 сообщений + скользящее summary |
| **Таймаут ответа** | backend 270 c / nginx 300 c (медленные ответы не рубятся) |
| **Параллелизм** | Ollama обрабатывает запросы по очереди (одна модель на CPU); несколько запросов не роняют сервис, а выстраиваются в очередь |

Проверено: доступ по сети (HTTPS), стабильность при нескольких одновременных запросах
(все `200`), срабатывание rate-limit (`429` сверх burst), соблюдение max context.

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
