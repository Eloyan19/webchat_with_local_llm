import json
import os
import re
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()

# Нативный Ollama endpoint (НЕ /v1/chat/completions OpenAI-compat) — нужен контроль
# контекста (num_ctx/seed/format), которого OpenAI-совместимый слой не даёт.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
# 3B на CPU медленный: grounded-ответ на 8 чанках кода (num_ctx 4096, до 512 токенов)
# заметно превышает дефолтные 120 c. Держим щедрый таймаут, конфигурируемый под железо.
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))
# Tunable-параметры генерации (перебираем при оптимизации под задачу). num_ctx —
# окно контекста (дефолт 2048 у Ollama обрезал бы чанки), num_predict — кап токенов
# ответа. PROMPT_VARIANT выбирает шаблон grounded-промпта: baseline | copyfirst.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "512"))
PROMPT_VARIANT = os.getenv("PROMPT_VARIANT", "baseline")
RAG_URL = os.getenv("RAG_URL", "http://127.0.0.1:8100")

# Plain RAG: single-stage retrieval. Improved RAG: retrieve more (k_before),
# rerank + threshold-filter, keep k_after. Идентично webchat/backend/main.py.
RAG_K = int(os.getenv("RAG_K", "5"))
RAG_K_BEFORE = int(os.getenv("RAG_K_BEFORE", "20"))
RAG_K_AFTER = int(os.getenv("RAG_K_AFTER", "8"))
# Relevance floor for the "don't know" gate. Preferred signal is the cross-encoder
# rerank_score returned by the RAG service (wide separation: in-domain ~ >=0,
# off-topic ~ -11); fallback to the compressed cosine score when the service does
# not rerank (in-domain ~0.66-0.73, off-topic ~0.5-0.59). A question whose best
# chunk sits below the floor gets an honest "не знаю" instead of a guessed answer.
RERANK_THRESHOLD = float(os.getenv("RERANK_THRESHOLD", "-6.0"))
RAG_SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.62"))

# Canned reply when retrieval finds nothing relevant enough, or when the model's
# cited attempt yielded no validated quote. We deliberately do NOT fall back to the
# model's own knowledge here — with RAG on, an unsupported answer is a hallucination
# risk, so we abstain and ask the user to refine.
ABSTAIN_REPLY = (
    "Не знаю: в найденных источниках нет ответа на этот вопрос. "
    "Уточните или переформулируйте вопрос."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Тёплый старт: прогоняем dummy-запрос через Ollama при старте приложения, чтобы
    # модель загрузилась в память ДО первого реального запроса пользователя (иначе
    # первый замер латентности платит за холодную загрузку весов). Не роняем старт,
    # если Ollama пока недоступна — systemd может поднимать юниты параллельно.
    try:
        await call_ollama([{"role": "user", "content": "hi"}], temperature=0)
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        pass
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    useRag: bool = False
    improvedRag: bool = False
    temperature: float = 0


class Source(BaseModel):
    file: str
    section: str
    score: float
    rerank_score: float | None = None
    # Стабильный id чанка из rag /search — сквозная адресация источника в UI.
    chunk_id: int | None = None
    # Verbatim fragment of this chunk that the model used, validated as a real
    # substring of the chunk text (anti-hallucination). None only on fallback paths.
    quote: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


async def call_ollama(
    messages: list[dict], temperature: float, json_mode: bool = False
) -> str:
    """Single chat completion через нативный Ollama `/api/chat` (НЕ OpenAI-compat —
    даёт контроль контекста: num_ctx/seed/format). Raises httpx errors / KeyError на
    неудаче — обрабатывает вызывающий код.

    num_ctx=4096 обязателен: дефолт Ollama (2048) обрежет промпт с 8 чанками кода и
    сломает корректность grounded-ответа. seed=42 — для сравнимости прогонов при
    разных temperature."""
    payload: dict = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": "10m",
        "options": {
            "temperature": temperature,
            "seed": 42,
            "top_p": 1,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    }
    if json_mode:
        payload["format"] = "json"
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        resp = await client.post(OLLAMA_URL, json=payload)
    resp.raise_for_status()
    return resp.json()["message"]["content"]


async def rewrite_query(messages: list[dict], last_user: str, temperature: float) -> str:
    """Rewrite the last question into a standalone retrieval query using dialog
    context (resolve pronouns/references). Falls back to last_user on any error."""
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[-6:])
    rewrite_msgs = [
        {
            "role": "system",
            "content": (
                "Ты переписываешь последний вопрос пользователя в ОДИН самостоятельный "
                "ВОПРОС для поиска по базе исходного кода (Google compose-samples). "
                "Раскрой местоимения и отсылки к прошлым репликам (напр. «этот класс» → "
                "конкретное имя из диалога), но СОХРАНИ форму естественного вопроса — НЕ "
                "превращай в набор ключевых слов (это ухудшает семантический поиск). "
                "Верни ТОЛЬКО вопрос, без кавычек и пояснений."
            ),
        },
        {
            "role": "user",
            "content": f"Диалог:\n{convo}\n\nПерепиши последний вопрос в поисковый запрос.",
        },
    ]
    try:
        rewritten = (await call_ollama(rewrite_msgs, temperature)).strip()
        return rewritten or last_user
    except (httpx.HTTPError, KeyError, ValueError):
        return last_user


async def rag_search(query: str, k: int, rerank: bool) -> list[dict]:
    """Call the RAG /search service. Returns chunks, or [] on any failure."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{RAG_URL}/search",
                json={"query": query, "k": k, "strategy": "structural", "rerank": rerank},
            )
        resp.raise_for_status()
        data = resp.json()
        return data.get("chunks", []) if isinstance(data, dict) else []
    except (httpx.HTTPError, ValueError):
        return []


def _number_chunks(chunks: list[dict]) -> str:
    """Нумерованный контекст [1..n] — общий для всех prompt-вариантов."""
    blocks = [
        f"[{i}] {c.get('file', '')} :: {c.get('section', '')}\n{c.get('text', '')}"
        for i, c in enumerate(chunks, start=1)
    ]
    return "\n\n".join(blocks)


def _prompt_baseline(context: str) -> str:
    """Исходный grounded-промпт (baseline для оптимизации)."""
    return (
        "Ты отвечаешь на вопрос пользователя, опираясь ТОЛЬКО на приведённый ниже "
        "контекст из базы знаний. Верни СТРОГО один JSON-объект такого вида:\n"
        '{"answer": "...", "used": [{"id": <номер источника>, "quote": "<дословный фрагмент>"}]}\n'
        "Правила:\n"
        "- answer: ответ на языке вопроса; ссылайся на источники по номеру в квадратных "
        "скобках, например [1].\n"
        "- used: для КАЖДОГО источника, на который опираешься, укажи id (номер [i]) и quote — "
        "фрагмент, СКОПИРОВАННЫЙ ПОБУКВЕННО из текста источника НА ЯЗЫКЕ ОРИГИНАЛА (обычно "
        "английский или код). НЕ переводи цитату на русский, не сокращай, не меняй пробелы, "
        "регистр и пунктуацию — quote должна дословно встречаться в тексте источника, иначе "
        "она будет отброшена. answer пиши на языке вопроса, но quote — только из оригинала.\n"
        "- Если в контексте НЕТ ответа на вопрос — верни в answer честное «Не знаю: в источниках "
        "нет ответа на этот вопрос, уточните вопрос» и пустой used: [].\n"
        "- Не выдумывай факты и цитаты вне контекста.\n\n"
        f"Контекст:\n{context}"
    )


def _prompt_copyfirst(context: str) -> str:
    """Copy-first вариант под слабую 3B: сперва ДОСЛОВНО скопировать строку в quote,
    потом писать answer. Один few-shot с кодовой цитатой; минимум лишних инструкций.
    Цель — снизить ложные абстейны из-за перефразированных/переведённых цитат."""
    return (
        "Отвечай ТОЛЬКО по контексту ниже. Работай в ДВА шага:\n"
        "1) Для каждого источника, который используешь, СКОПИРУЙ из его текста короткий "
        "фрагмент СИМВОЛ-В-СИМВОЛ — как есть, на языке оригинала (обычно код/английский). "
        "НЕ переводи, НЕ перефразируй, НЕ меняй регистр, пробелы и пунктуацию. Это quote.\n"
        "2) Потом сформулируй answer на языке вопроса, опираясь на эти quote; ссылайся на "
        "источники по номеру [i].\n"
        "Верни СТРОГО один JSON:\n"
        '{"answer": "...", "used": [{"id": <номер>, "quote": "<точная подстрока источника>"}]}\n\n'
        "Пример формата:\n"
        "Контекст: [1] Foo.kt :: class Foo\nclass Foo(val bar: Int) { fun baz() = bar * 2 }\n"
        "Вопрос: Что делает baz()?\n"
        'Ответ: {"answer": "baz() возвращает bar, умноженный на 2 [1].", '
        '"used": [{"id": 1, "quote": "fun baz() = bar * 2"}]}\n\n'
        "Важно: quote обязана ДОСЛОВНО встречаться в тексте своего источника, иначе она "
        "отбрасывается. Не можешь скопировать дословно — не цитируй этот источник. Если "
        "ответа в контексте нет — answer «Не знаю: в источниках нет ответа» и used: [].\n\n"
        f"Контекст:\n{context}"
    )


def build_system_prompt(chunks: list[dict]) -> str:
    """System prompt, пиннящий модель к контексту и требующий cited JSON. Шаблон
    выбирается через PROMPT_VARIANT (baseline | copyfirst) — рычаг оптимизации."""
    context = _number_chunks(chunks)
    if PROMPT_VARIANT == "copyfirst":
        return _prompt_copyfirst(context)
    return _prompt_baseline(context)


# Типографские варианты, которые модель (и исходный текст) может использовать
# взаимозаменяемо: кавычки-«ёлочки»/угловые/типографские -> ", длинное/короткое
# тире -> дефис, ё -> е. Симметрично: применяется к цитате И к тексту чанка, так
# что реальное совпадение не ломается на форматировании, а не-совпадение
# (выдуманная цитата) по-прежнему отбрасывается.
_TYPOGRAPHY_MAP = str.maketrans({
    "»": '"', "«": '"', "“": '"', "”": '"',
    "‘": "'", "’": "'",
    "—": "-", "–": "-",
    "ё": "е",
})


def _normalize(text: str) -> str:
    """Collapse whitespace, lowercase, and normalize typography so quote validation
    tolerates trivial reflow/formatting differences between the model's copy and the
    raw chunk text."""
    text = text.lower().translate(_TYPOGRAPHY_MAP)
    return re.sub(r"\s+", " ", text).strip()


def parse_grounded_reply(
    raw: str, chunks: list[dict]
) -> tuple[str, list[Source], int, bool]:
    """Parse the model's JSON reply into (answer, validated_sources, quotes_dropped,
    json_ok).

    json_ok=False means `raw` was not valid JSON (or not a JSON object) — Ollama's 3B
    model occasionally ignores `format: json`. The caller retries once on this and
    surfaces it via ragMeta.jsonParseFailed. Each `used` entry is kept only if its id
    maps to a presented chunk AND its quote is a real substring of that chunk's text
    (normalized) — a fabricated quote is dropped.
    """
    by_id = {i: c for i, c in enumerate(chunks, start=1)}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw.strip(), [], 0, False
    if not isinstance(data, dict):
        return raw.strip(), [], 0, False

    answer = str(data.get("answer", "")).strip()
    used = data.get("used", []) or []
    if not answer:
        answer = raw.strip()

    sources: list[Source] = []
    dropped = 0
    seen: set[int] = set()
    for item in used if isinstance(used, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        try:
            cid = int(item.get("id"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        quote = str(item.get("quote", "")).strip()
        chunk = by_id.get(cid)
        if chunk is None or not quote:
            dropped += 1
            continue
        if _normalize(quote) not in _normalize(chunk.get("text", "")):
            dropped += 1  # quote not found verbatim in the chunk -> hallucinated
            continue
        if cid in seen:
            continue
        seen.add(cid)
        sources.append(Source(
            file=chunk.get("file", ""), section=chunk.get("section", ""),
            score=chunk.get("score", 0.0), rerank_score=chunk.get("rerank_score"),
            chunk_id=chunk.get("chunk_id"), quote=quote,
        ))
    return answer, sources, dropped, True


# Prepended (as an extra system turn) on a retry when the first cited attempt yielded
# no validated quote — reminds the model to copy the quote verbatim, in the source's
# original language, instead of paraphrasing or translating it.
QUOTE_RETRY_NUDGE = (
    "ВАЖНО: в прошлый раз ни одна цитата не совпала с текстом источника дословно. "
    "Скопируй каждую quote ПОБУКВЕННО из текста источника на ЯЗЫКЕ ОРИГИНАЛА "
    "(не переводи, не сокращай, не меняй пробелы и регистр). Если дословной цитаты "
    "действительно нет — верни used: []."
)

# Prepended on a retry when the first attempt was not valid JSON at all (qwen2.5:3b
# occasionally wraps the reply in markdown fences or adds prose despite format=json).
JSON_RETRY_NUDGE = (
    "ВАЖНО: прошлый ответ не был валидным JSON-объектом. Верни СТРОГО один JSON-объект "
    'вида {"answer": "...", "used": [...]}, без markdown-разметки (без ```), без '
    "пояснений до или после — только сам JSON."
)


async def generate_grounded(
    messages: list[dict], chunks: list[dict], temperature: float
) -> tuple[str, list[Source], int, bool, bool]:
    """Cited JSON generation with ONE retry on empty quotes OR invalid JSON.

    The verbatim-quote gate occasionally false-abstains: retrieval found the answer
    but the model paraphrased/translated its quote (or, for this 3B local model,
    returned malformed JSON despite format=json). Before giving up we retry once with
    a firmer instruction. Returns (reply, sources, quotes_dropped, retried,
    json_parse_failed). messages[0] is the context system prompt; the nudge goes right
    after it."""
    raw = await call_ollama(messages, temperature, json_mode=True)
    reply, sources, dropped, json_ok = parse_grounded_reply(raw, chunks)
    json_parse_failed = not json_ok
    if sources:
        return reply, sources, dropped, False, json_parse_failed

    nudge = QUOTE_RETRY_NUDGE if json_ok else JSON_RETRY_NUDGE
    retry_messages = [
        messages[0], {"role": "system", "content": nudge}, *messages[1:]
    ]
    raw = await call_ollama(retry_messages, temperature, json_mode=True)
    reply, sources, dropped, json_ok2 = parse_grounded_reply(raw, chunks)
    json_parse_failed = json_parse_failed or not json_ok2
    return reply, sources, dropped, True, json_parse_failed


async def fetch_rag_context(query: str, improved: bool) -> tuple[list[dict], dict]:
    """Retrieve and relevance-gate chunks for `query`.

    Plain mode: single-stage top-RAG_K, cosine floor. Improved mode: retrieve
    RAG_K_BEFORE (asking the service to rerank), floor on rerank_score, keep top
    RAG_K_AFTER. Returns (kept_chunks, meta). Empty kept_chunks -> the caller
    abstains ("не знаю"). We never silently answer from the model's own knowledge.
    """
    k = RAG_K_BEFORE if improved else RAG_K
    chunks = await rag_search(query, k, rerank=improved)
    meta: dict = {"k_requested": k, "k_returned": len(chunks), "improved": improved}
    if not chunks:
        meta["abstained"] = True
        return [], meta

    # Floor on rerank_score when the service provides it (cleaner separation),
    # else on the cosine score.
    has_rerank = chunks[0].get("rerank_score") is not None
    signal, threshold = (
        ("rerank_score", RERANK_THRESHOLD) if has_rerank
        else ("score", RAG_SIMILARITY_THRESHOLD)
    )
    kept = [c for c in chunks if c.get(signal, 0.0) >= threshold]
    if improved:
        kept = kept[:RAG_K_AFTER]
    meta.update({
        "filter_signal": signal, "threshold": threshold,
        "k_after_filter": len(kept), "filtered_out": len(chunks) - len(kept),
        "abstained": len(kept) == 0,
    })
    return kept, meta


@app.post("/chat")
async def chat(req: ChatRequest):
    messages = [m.model_dump() for m in req.messages]
    sources: list[Source] = []
    rewritten_query: str | None = None
    rag_meta: dict = {}
    grounded_chunks: list[dict] | None = None

    if req.useRag:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), None
        )
        if last_user:
            search_query = last_user
            # Query rewrite only helps when there is prior dialog to resolve
            # (pronouns/references); on a standalone question it adds noise.
            if req.improvedRag and len(messages) > 1:
                search_query = await rewrite_query(messages, last_user, req.temperature)
                rewritten_query = search_query
            grounded_chunks, rag_meta = await fetch_rag_context(
                search_query, req.improvedRag
            )
            # Relevance gate: nothing passed the floor -> abstain, do not generate.
            if rag_meta.get("abstained"):
                return {
                    "reply": ABSTAIN_REPLY,
                    "sources": [],
                    "rewrittenQuery": rewritten_query,
                    "ragMeta": rag_meta,
                }
            messages = [
                {"role": "system", "content": build_system_prompt(grounded_chunks)},
                *messages,
            ]

    try:
        if grounded_chunks:
            reply, sources, dropped, retried, json_parse_failed = await generate_grounded(
                messages, grounded_chunks, req.temperature
            )
            rag_meta["quotesDropped"] = dropped
            rag_meta["retried"] = retried
            rag_meta["jsonParseFailed"] = json_parse_failed
            # No validated quote survived even after the retry (model paraphrased/
            # hallucinated, cited nothing, or never returned valid JSON). We must not
            # surface an answer with dangling [i] refs and no sources — abstain to
            # keep the "every answer is grounded in a real quote" guarantee.
            if not sources:
                rag_meta["abstained"] = True
                rag_meta["modelAbstained"] = True
                return {
                    "reply": ABSTAIN_REPLY,
                    "sources": [],
                    "rewrittenQuery": rewritten_query,
                    "ragMeta": rag_meta,
                }
        else:
            reply = await call_ollama(messages, req.temperature)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"Ollama request failed: {e}"})
    except (KeyError, ValueError) as e:
        return JSONResponse(status_code=502, content={"error": f"Bad Ollama response: {e}"})

    return {
        "reply": reply,
        "sources": [s.model_dump() for s in sources],
        "rewrittenQuery": rewritten_query,
        "ragMeta": rag_meta,
    }
