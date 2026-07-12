import { useEffect, useState } from 'react'
import { MAX_CONTEXT, sendChat, summarize } from './api'
import type { Message } from './types'
import './App.css'

const STORAGE_KEY = 'webchat.messages'
const SUMMARY_KEY = 'webchat.summary'
const SUMMARIZED_COUNT_KEY = 'webchat.summarizedCount'
const USE_RAG_KEY = 'webchat.useRag'
const TEMPERATURE_KEY = 'webchat.temperature'

function loadMessages(): Message[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as Message[]) : []
  } catch {
    return []
  }
}

function loadUseRag(): boolean {
  const raw = localStorage.getItem(USE_RAG_KEY)
  return raw === null ? true : raw === 'true'
}

function loadTemperature(): number {
  const raw = Number(localStorage.getItem(TEMPERATURE_KEY))
  return raw === 1 ? 1 : 0
}

function App() {
  const [messages, setMessages] = useState<Message[]>(loadMessages)
  const [summary, setSummary] = useState<string>(
    () => localStorage.getItem(SUMMARY_KEY) ?? '',
  )
  const [summarizedCount, setSummarizedCount] = useState<number>(
    () => Number(localStorage.getItem(SUMMARIZED_COUNT_KEY)) || 0,
  )
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // RAG всегда включён по умолчанию — источники сразу видны.
  const [useRag, setUseRag] = useState<boolean>(loadUseRag)
  // Температура генерации: 0 — детерминированно/точно, 1 — креативно.
  const [temperature, setTemperature] = useState<number>(loadTemperature)

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages))
  }, [messages])

  useEffect(() => {
    localStorage.setItem(SUMMARY_KEY, summary)
  }, [summary])

  useEffect(() => {
    localStorage.setItem(SUMMARIZED_COUNT_KEY, String(summarizedCount))
  }, [summarizedCount])

  useEffect(() => {
    localStorage.setItem(USE_RAG_KEY, String(useRag))
  }, [useRag])

  useEffect(() => {
    localStorage.setItem(TEMPERATURE_KEY, String(temperature))
  }, [temperature])

  async function handleSend() {
    const text = input.trim()
    if (!text || loading) return

    const userMsg: Message = { role: 'user', content: text, ts: Date.now() }
    const next = [...messages, userMsg]
    setMessages(next)
    setInput('')
    setError(null)
    setLoading(true)

    try {
      // Fold messages that fell out of the takeLast(MAX_CONTEXT) window into a
      // running summary so the model keeps early facts. Failure here is
      // non-fatal — we just send without an updated summary.
      let curSummary = summary
      const dropTo = Math.max(0, next.length - MAX_CONTEXT)
      if (dropTo > summarizedCount) {
        try {
          curSummary = await summarize(curSummary, next.slice(summarizedCount, dropTo))
          setSummary(curSummary)
          setSummarizedCount(dropTo)
        } catch {
          /* keep previous summary; older messages just drop out of context */
        }
      }

      const { reply, sources } = await sendChat(next, useRag, temperature, curSummary)
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: reply, ts: Date.now(), sources },
      ])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function handleClear() {
    if (messages.length === 0) return
    if (!window.confirm('Очистить всю историю чата? Это действие необратимо.')) return
    setMessages([])
    setSummary('')
    setSummarizedCount(0)
    setError(null)
    localStorage.removeItem(STORAGE_KEY)
    localStorage.removeItem(SUMMARY_KEY)
    localStorage.removeItem(SUMMARIZED_COUNT_KEY)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="chat">
      <header className="chat-header">
        <h1>Web Chat · Local LLM (qwen2.5:3b)</h1>
        <button
          type="button"
          className="clear"
          onClick={handleClear}
          disabled={messages.length === 0}
        >
          Очистить
        </button>
      </header>

      <div className="messages">
        {messages.length === 0 && (
          <p className="empty">Напиши сообщение, чтобы начать.</p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg msg-${m.role}`}>
            <span className="role">{m.role === 'user' ? 'Вы' : 'Модель'}</span>
            <span className="content">{m.content}</span>
            {m.sources && m.sources.length > 0 && (
              <div className="sources">
                <span className="sources-title">Источники:</span>
                <ol>
                  {m.sources.map((s, j) => (
                    <li key={j}>
                      <code>{s.file}</code> :: {s.section}
                      {s.chunk_id != null && (
                        <span className="chunk-id"> #{s.chunk_id}</span>
                      )}
                      {s.quote && <blockquote className="quote">{s.quote}</blockquote>}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        ))}
        {loading && <div className="msg msg-assistant">…</div>}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="controls">
        <label className="rag-toggle">
          <input
            type="checkbox"
            checked={useRag}
            onChange={(e) => setUseRag(e.target.checked)}
          />
          RAG (поиск по базе знаний)
        </label>

        <div className="temp-control" role="group" aria-label="Температура">
          <span className="temp-label">Температура:</span>
          <div className="temp-segment">
            <button
              type="button"
              className={temperature === 0 ? 'temp-option active' : 'temp-option'}
              onClick={() => setTemperature(0)}
              aria-pressed={temperature === 0}
            >
              0 · точно
            </button>
            <button
              type="button"
              className={temperature === 1 ? 'temp-option active' : 'temp-option'}
              onClick={() => setTemperature(1)}
              aria-pressed={temperature === 1}
            >
              1 · креативно
            </button>
          </div>
        </div>
      </div>

      <div className="composer">
        <input
          type="text"
          value={input}
          placeholder="Сообщение…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={loading}
        />
        <button type="button" onClick={handleSend} disabled={loading || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  )
}

export default App
