import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()

# OpenAI-совместимый эндпоинт Ollama. Без Authorization — модель локальная,
# ключ не нужен.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/v1/chat/completions")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

app = FastAPI()

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


@app.get("/health")
def health():
    return {"ok": True}


async def call_ollama(messages: list[dict]) -> str:
    """Single chat completion через локальный Ollama (OpenAI-совместимый /v1).
    Raises httpx errors / KeyError на неудаче — обрабатывает вызывающий код."""
    payload = {"model": OLLAMA_MODEL, "messages": messages}
    # Таймаут большой: 3B на CPU (VPS без GPU) генерирует медленно.
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(OLLAMA_URL, json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


@app.post("/chat")
async def chat(req: ChatRequest):
    messages = [m.model_dump() for m in req.messages]
    try:
        reply = await call_ollama(messages)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"Ollama request failed: {e}"})
    except (KeyError, ValueError) as e:
        return JSONResponse(status_code=502, content={"error": f"Bad Ollama response: {e}"})
    return {"reply": reply}
