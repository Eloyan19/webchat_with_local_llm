import { useEffect, useState } from 'react'
import { MAX_CONTEXT, sendChat, summarize } from './api'
import type { Message } from './types'
import './App.css'

const STORAGE_KEY = 'webchat.messages'
const SUMMARY_KEY = 'webchat.summary'
const SUMMARIZED_COUNT_KEY = 'webchat.summarizedCount'

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

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages))
  }, [messages])

  useEffect(() => {
    localStorage.setItem(SUMMARY_KEY, summary)
  }, [summary])

  useEffect(() => {
    localStorage.setItem(SUMMARIZED_COUNT_KEY, String(summarizedCount))
  }, [summarizedCount])

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

      const { reply } = await sendChat(next, curSummary)
      setMessages((prev) => [...prev, { role: 'assistant', content: reply, ts: Date.now() }])
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
          </div>
        ))}
        {loading && <div className="msg msg-assistant">…</div>}
      </div>

      {error && <div className="error">{error}</div>}

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
