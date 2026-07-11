import type { Message } from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'
const CHAT_TOKEN = import.meta.env.VITE_CHAT_TOKEN ?? ''
export const MAX_CONTEXT = 20

export interface ChatResult {
  reply: string
}

type WireMessage = { role: string; content: string }

function headers(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  if (CHAT_TOKEN) h.Authorization = `Bearer ${CHAT_TOKEN}`
  return h
}

async function postChat(messages: WireMessage[]): Promise<ChatResult> {
  const res = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ messages }),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`Backend error ${res.status}: ${text}`)
  }
  const data = await res.json()
  return {
    reply: data.reply as string,
  }
}

export async function sendChat(messages: Message[], summary: string = ''): Promise<ChatResult> {
  const window = messages.slice(-MAX_CONTEXT)
  const wire: WireMessage[] = window.map((m) => ({ role: m.role, content: m.content }))
  if (summary) {
    wire.unshift({
      role: 'system',
      content: `Краткое содержание более ранней части диалога:\n${summary}`,
    })
  }
  console.log(
    `[chat] sending ${wire.length} messages (history ${messages.length}, summary=${summary ? 'yes' : 'no'})`,
  )
  return postChat(wire)
}

// Condense the messages that fell out of the takeLast(MAX_CONTEXT) window into a
// running summary, folding in any previous summary. One chat-completion call.
export async function summarize(previousSummary: string, dropped: Message[]): Promise<string> {
  const convo = dropped.map((m) => `${m.role}: ${m.content}`).join('\n')
  const prompt =
    `Существующее summary диалога:\n${previousSummary || '(пусто)'}\n\n` +
    `Новые сообщения, которые нужно добавить в summary:\n${convo}\n\n` +
    `Обнови summary: в 3–6 предложениях сохрани ключевые факты, решения, имена и числа ` +
    `из всего диалога. Верни только текст summary, без пояснений.`
  const { reply } = await postChat([{ role: 'user', content: prompt }])
  return reply.trim()
}
