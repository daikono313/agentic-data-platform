/** POST /sessions/{id}/feedback の型（backend の adp.feedback と対応） */
export interface FeedbackIn {
  kind: 'message' | 'session'
  message_index?: number | null
  thumb?: 'up' | 'down' | null
  score?: number | null
  categories?: string[]
  comment?: string
}

export interface FeedbackView {
  kind: 'message' | 'session'
  message_index: number | null
  thumb: 'up' | 'down' | null
  score: number | null
  categories: string[]
  comment: string
  prompt_version: string | null
  model: string | null
}

/** 分類のキー → 表示名（GET /feedback/categories） */
export type FeedbackCategories = Record<string, string>

export const MAX_COMMENT = 2000

export function findFeedback(list: FeedbackView[] | undefined, kind: 'message' | 'session', index?: number) {
  return list?.find((f) => f.kind === kind && (kind === 'session' || f.message_index === index))
}

/** 保存したフィードバックを一覧に反映する（同じ発言・セッションのものは置き換える） */
export function mergeFeedback(list: FeedbackView[] | undefined, item: FeedbackView): FeedbackView[] {
  const rest = (list ?? []).filter((f) => !(f.kind === item.kind && f.message_index === item.message_index))
  return [...rest, item]
}
