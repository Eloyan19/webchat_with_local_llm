# CLAUDE.md — Web Chat · Local LLM (Ollama)

## Среда
- Claude Code работает на VPS **jorchik.com**. Деплой: systemd + nginx, поддомен
  **llm.jorchik.com** (отдельный `server`-блок, свой Certbot-сертификат) — рядом с
  обычным `webchat` на `jorchik.com`, но полностью изолирован (свой порт/юнит/лог).
- **RAM VPS 3.8 ГБ** → локальные Ollama-модели ≤3B. Модель по умолчанию —
  `qwen2.5:3b` (уже загружена в Ollama на `:11434`, ~2 ГБ). Сильнее не тянем.
- Соседние репозитории: `../webchat/` — тот же UI поверх **облачного DeepSeek**
  (свой репо/CLAUDE.md); `../rag/` — RAG-пайплайн; `../AI_Challenge_2_3_4_5/` —
  Android, **⏸ приостановлен, не трогать**.

## Проект
Минимальный веб-чат поверх **локальной** LLM (Ollama). Тот же интерфейс, что у
`webchat`, но:
- **Без RAG, без цитат, без «памяти задачи»** — намеренно минимально. Новизна тут
  в локальной модели, а не в грундинге. RAG можно надстроить позже отдельным заходом.
- История — localStorage + скользящее summary (сжатие контекста).
**Стек:** TypeScript · React · Vite · Python · FastAPI · httpx · localStorage.

## Архитектура (инварианты)
- Браузер → свой backend → **Ollama** (`OLLAMA_URL`, по умолчанию
  `http://127.0.0.1:11434/v1/chat/completions`, OpenAI-совместимый эндпоинт).
  Браузер НЕ ходит в Ollama напрямую (единый origin, backend-прокси).
- Ключи не нужны — модель локальная. `OLLAMA_MODEL` / `OLLAMA_URL` — через env/`.env`.
- Модель по умолчанию `qwen2.5:3b`. 3B на CPU **медленный** → httpx-таймаут 180 c,
  nginx `proxy_read_timeout 180s`.
- Контракт `/chat`: `POST {messages:[{role,content}]} → {reply}`. Минимальный —
  без useRag/sessionId/sources. `/health → {ok:true}`.
- Gate-токен `/chat` (`VITE_CHAT_TOKEN` ↔ nginx) — как в webchat, отсекает
  случайный абьюз (жжёт CPU). Отдельный токен и отдельная rate-limit зона
  `chat_local` (не пересекается с зоной `chat` webchat).

## Деплой (см. deploy/README.md)
- backend: `webchat-local.service` (systemd), uvicorn `127.0.0.1:8010`,
  `After/Wants ollama.service`.
- frontend: прод-сборка Vite (same-origin, `VITE_API_BASE=`) в `/var/www/webchat-local`.
- nginx: отдельный `server`-блок `llm.jorchik.com` (`deploy/nginx-llm.conf`), TLS
  через `certbot --nginx -d llm.jorchik.com`. Нужна DNS A-запись на поддомен.
- **Порты:** этот backend `:8010`; заняты: webchat `:8000`, MCP `:8001/:8002`,
  rag `:8100`, Ollama `:11434`.

## Роль Claude — советник
Пользователь изучает LLM/агентов/веб-разработку. Подмечай упущенные паттерны Claude
Code (агенты, worktree, /loop, фоновые задачи) и ограничения решения. Одно короткое
замечание в конце — достаточно, не лекция.

## Агенты (persona-агенты через Agent(model:, prompt:))

| Агент | Модель | Когда |
|---|---|---|
| 🏗️ ARCHITECT | opus | структура, границы фронт/backend, выбор паттерна |
| ⚛️ FRONTEND DEVELOPER | sonnet | React, TypeScript, hooks, state, Vite |
| ⚙️ BACKEND DEVELOPER | sonnet | Python/FastAPI, httpx, async, Ollama-прокси; ревьюит Python |
| 🔍 CODE REVIEWER | sonnet | ревью TS/React |
| 🎨 UI/UX SPECIALIST | sonnet | адаптивный CSS, a11y, UX |
| 🛡️ SECURITY AUDITOR | opus | утечка токена, CORS, инъекции, rate-limit |
| 🧠 LLM ENGINEER | opus | промпт, управление контекстом, Ollama-параметры |
| 🧪 QA ENGINEER | sonnet | Vitest (unit), Playwright (e2e) |
| 🐛 DEBUG SPECIALIST | opus | root-cause анализ багов |

**Ревьюер:** TS/React → CODE REVIEWER; Python → BACKEND DEVELOPER. **fresh agent**
(`subagent_type: "claude"`) с самодостаточным промптом предпочтительнее fork.
