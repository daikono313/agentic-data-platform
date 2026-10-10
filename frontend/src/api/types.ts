import type { BeamModel, EventMatrix } from '@/generated/beam'

export type Phase =
  | 'framing'
  | 'event_discovery'
  | 'seven_ws'
  | 'grain'
  | 'conformance'
  | 'prioritization'
  | 'review'
  | 'finalized'

export const PHASES: { id: Phase; label: string }[] = [
  { id: 'framing', label: '要求の言い換え' },
  { id: 'event_discovery', label: 'イベントの発見' },
  { id: 'seven_ws', label: '7W の深掘り' },
  { id: 'grain', label: '粒度と集計方法' },
  { id: 'conformance', label: '共通ディメンション' },
  { id: 'prioritization', label: '優先度' },
  { id: 'review', label: '確認' },
  { id: 'finalized', label: '確定' },
]

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  /** 選択肢つきの質問文。本文とは別に持つ（#36） */
  question?: string | null
  choices?: string[]
  at?: string
}

export interface JobStep {
  step: string
  label: string
  status: 'pending' | 'running' | 'done' | 'failed'
  detail?: string
}

export interface JobView {
  id: string
  status: 'running' | 'succeeded' | 'failed'
  steps: JobStep[]
  pr_url?: string | null
  error?: string | null
  /** 生成ジョブの作業ブランチ（PR の内容をマージ前に描画画面で見るため） */
  branch?: string | null
}

export interface SessionView {
  id: string
  phase: Phase
  beam: BeamModel
  matrix: EventMatrix
  issues: string[]
  choices: string[]
  question?: string | null
  messages: ChatMessage[]
  job?: JobView | null
}

/** POST /sessions/{id}/messages の SSE イベント */
export type InterviewEvent =
  | { event: 'text'; data: { text: string } }
  | { event: 'tool'; data: { name: string } }
  | { event: 'beam'; data: { beam: BeamModel; matrix: EventMatrix; issues: string[] } }
  | { event: 'phase'; data: { phase: Phase } }
  | { event: 'choices'; data: { question?: string | null; options: string[] } }
  | { event: 'done'; data: { message: ChatMessage; error?: string | null } }

/** GET /jobs/{id}/events の SSE イベント */
export type JobEvent =
  | { event: 'step'; data: JobStep }
  | { event: 'done'; data: JobView }
