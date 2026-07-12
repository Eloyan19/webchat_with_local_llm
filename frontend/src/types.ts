export type Role = 'user' | 'assistant'

export interface Source {
  file: string
  section: string
  score: number
  rerank_score?: number
  // Стабильный id чанка из контракта rag /search — сквозная адресация в UI.
  chunk_id?: number
  // Дословный фрагмент чанка, на который опирается ответ (валидируется на backend).
  quote?: string
}

export interface Message {
  role: Role
  content: string
  ts: number
  sources?: Source[]
}
