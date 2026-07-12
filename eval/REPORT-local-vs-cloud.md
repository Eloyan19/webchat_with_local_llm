# Локальная (qwen2.5:3b) vs облачная (DeepSeek) RAG-система — отчёт eval

Генерируется `compare_local_vs_cloud.py`. Один и тот же корпус вопросов (compose-samples, 12 in-domain + 5 off-topic) прогнан против двух систем с одинаковым контрактом `POST /chat` (`useRag=true, improvedRag=true`): **cloud** — backend на DeepSeek (`:8000`), **local** — backend на Ollama qwen2.5:3b (`:8010`). Faithfulness судит DeepSeek через `:8000` в no-RAG режиме — один и тот же слепой арбитр для обеих систем.

## Сводная таблица

| Метрика | cloud t0 | cloud t1 | local t0 | local t1 |
|---|---|---|---|---|
| Grounded (судья), доля от отвеченных | 83% | 100% | 71% | 57% |
| Источники присутствуют | 100% | 92% | 64% | 58% |
| Retrieval hit (expected_sources) | 100% | 92% | 55% | 50% |
| Off-topic abstain correct | 100% | 100% | 100% | 100% |
| Latency mean | 8.3с | 7.8с | 144.8с | 73.9с |
| Latency p50 | 8.9с | 7.6с | 134.4с | 45.1с |
| Latency p95 | 9.8с | 10.3с | 249.6с | 172.9с |
| Стабильность abstain↔answer (t=1) | — | 83% | — | 83% |
| quotesDropped mean (t=1) | — | 0.03 | — | 0.56 |
| quotesDropped max (t=1) | — | 1 | — | 3 |
| jsonParseFailed count (t=1) | 0 | 0 | 0 | 3 |

## Выводы

**Качество (grounded / retrieval / abstain).** Cloud: grounded 83% (t0) / 100% (t1), retrieval hit 100% (t0). Local: grounded 71% (t0) / 57% (t1), retrieval hit 55% (t0). Off-topic abstain-correct — cloud 100%, local 100%.

**Скорость.** Cloud latency mean 8.3с (t0), local 144.8с (t0) — локальная модель на CPU ожидаемо на порядок медленнее облачной.

**Стабильность (temp=1, повторы).** Cloud: consistent abstain↔answer 83%, quotesDropped mean 0.03, jsonParseFailed 0. Local: consistent 83%, quotesDropped mean 0.56, jsonParseFailed 3.

## Примеры (вопрос → ответ cloud vs local)

### 1. In Jetchat's ConversationUiState, what does the addMessage(msg) function do — at which position in the list is a new message inserted?

**cloud** (8.89с, abstained=False)

> The `addMessage(msg)` function inserts a new message at the **beginning** of the list, as stated by the comment: `// Add to the beginning of the list` [1].

_источники: Jetchat/app/src/main/java/com/example/compose/jetchat/conversation/ConversationUiState.kt_

**local** (39.78с, abstained=True)

> Не знаю: в найденных источниках нет ответа на этот вопрос. Уточните или переформулируйте вопрос.

_источники: нет_

---

### 2. What constructor parameters does Jetchat's ConversationUiState class take?

**cloud** (9.01с, abstained=False)

> Jetchat's ConversationUiState class constructor takes three parameters: `channelName` (String), `channelMembers` (Int), and `initialMessages` (List<Message>). [4]

_источники: Jetchat/app/src/main/java/com/example/compose/jetchat/conversation/ConversationUiState.kt_

**local** (105.7с, abstained=True)

> Не знаю: в найденных источниках нет ответа на этот вопрос. Уточните или переформулируйте вопрос.

_источники: нет_

---

### 3. Which UI tests are included in the Jetchat androidTest suite, and what does each one cover?

**cloud** (9.6с, abstained=False)

> The Jetchat androidTest suite includes three UI tests: [1] ConversationTest — UI tests for the Conversation screen, including a check for behavior when dark mode changes [2]; [2] UserInputTest — checks that user input composables and extended controls behave as expected, including showing and hiding the keyboard [3]; and [3] NavigationTest — shows how to write tests that assert directly on the Navigation Controller [4].

_источники: Jetchat/README.md, Jetchat/README.md, Jetchat/README.md_

**local** (155.21с, abstained=True)

> Не знаю: в найденных источниках нет ответа на этот вопрос. Уточните или переформулируйте вопрос.

_источники: нет_

---

## Оговорки методологии

- **Retrieval идентичен обеим системам** (один сервис `rag:8100`, тот же индекс). Метрика
  «retrieval hit» здесь считается по ФИНАЛЬНЫМ источникам ответа, поэтому при абстейне
  (source=[]) она падает — то есть отражает «смогла ли модель выдать валидную цитату»,
  а не «нашёл ли поиск». Различие cloud/local по этой строке — про генерацию, не про поиск.
- **Повторов поровну: N=3 при temp=1** для обеих систем (N=1 при temp=0 — почти детерминировано).
- **Судья облачный** (DeepSeek) — вынесен за пределы сравнения (обе системы судит один слепой
  арбитр). Сама RAG-система (retrieval + генерация) у local полностью локальна.
- **Железо:** VPS 3.8 ГБ, CPU-only. qwen2.5:3b @ num_ctx=4096 упирался в RAM (Ollama падала
  по OOM-killer) — добавлен 4 ГБ swap. Отсюда высокая и разбросанная латентность local.
