import type { ChartOptions, ChartRequest, ChartResult } from './charts'
import type {
  CleansingSpec,
  CleansingView,
  Datasets,
  OnboardingDraft,
  OntologyPatch,
  OntologyView,
  SourceProfile,
} from './datasets'
import type { FeedbackCategories, FeedbackIn, FeedbackView } from './feedback'
import type { InterviewEvent, JobEvent, JobView, SessionSummary, SessionView, SyncView } from './types'

const BASE = '/api'

/**
 * API のエラーの本文（FastAPI の {"detail": ...}）から、画面に出す文を取り出す。
 * detail が入力の検証エラーの一覧なら、項目名と msg をつなぐ。JSON でなければそのまま
 */
export function detailText(body: string): string {
  let detail: unknown
  try {
    detail = (JSON.parse(body) as { detail?: unknown } | null)?.detail
  } catch {
    return body
  }
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    // 入力の検証エラー（422）。どの項目かが分かるように loc の末尾を付ける
    const msgs = detail.map((d: unknown) => {
      if (!d || typeof d !== 'object' || !('msg' in d)) return JSON.stringify(d)
      const loc = 'loc' in d && Array.isArray(d.loc) ? d.loc.at(-1) : undefined
      return loc !== undefined ? `${String(loc)}: ${String(d.msg)}` : String(d.msg)
    })
    if (msgs.length) return `入力が正しくありません（${msgs.join(' / ')}）`
  }
  return body
}

/** 失敗した応答を、detail を取り出したエラーにする（画面に生の JSON を出さない） */
async function apiError(res: Response): Promise<Error> {
  const body = await res.text().catch(() => '')
  return new Error(body ? detailText(body) : `${res.status} ${res.statusText}`)
}

/** 例外を画面に出す文にする（`Error: ` を付けない。API のエラーは detail だけ） */
export function errorText(e: unknown): string {
  return detailText(e instanceof Error ? e.message : String(e))
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiError(res)
  return (await res.json()) as T
}

/** text/event-stream を読み、`event:` と `data:`（JSON）の組を順に返す */
export async function* readSSE<T extends { event: string; data: unknown }>(
  res: Response,
): AsyncGenerator<T> {
  if (!res.ok || !res.body) throw await apiError(res)
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    let sep: number
    while ((sep = buffer.search(/\r?\n\r?\n/)) >= 0) {
      const chunk = buffer.slice(0, sep)
      buffer = buffer.slice(sep).replace(/^\r?\n\r?\n/, '')
      let event = 'message'
      const data: string[] = []
      for (const line of chunk.split(/\r?\n/)) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      }
      if (data.length) yield { event, data: JSON.parse(data.join('\n')) } as T
    }
  }
}

export const api = {
  async datasets(): Promise<Datasets> {
    return json(await fetch(`${BASE}/datasets`))
  },
  async ontology(): Promise<OntologyView> {
    return json(await fetch(`${BASE}/ontology`))
  },
  async onboardingDraft(table: string, agent: boolean): Promise<OnboardingDraft> {
    return json(
      await fetch(`${BASE}/sources/${encodeURIComponent(table)}/onboarding/draft`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent }),
      }),
    )
  },
  async startOnboarding(
    table: string,
    patch: OntologyPatch,
    cleansing: string | CleansingSpec,
    redo = false,
  ): Promise<JobView> {
    return json(
      await fetch(`${BASE}/sources/${encodeURIComponent(table)}/onboarding`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patch, cleansing, redo }),
      }),
    )
  },
  async onboardingStatus(table: string): Promise<{ job: JobView | null }> {
    return json(await fetch(`${BASE}/sources/${encodeURIComponent(table)}/onboarding`))
  },
  async sourceCleansing(table: string): Promise<CleansingView> {
    return json(await fetch(`${BASE}/sources/${encodeURIComponent(table)}/cleansing`))
  },
  async sourceProfile(table: string): Promise<SourceProfile> {
    return json(await fetch(`${BASE}/sources/${encodeURIComponent(table)}/profile`))
  },
  async createSession(request: string): Promise<SessionView> {
    return json(
      await fetch(`${BASE}/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request }),
      }),
    )
  },
  async history(): Promise<{ sessions: SessionSummary[] }> {
    return json(await fetch(`${BASE}/sessions`))
  },
  async syncStatus(): Promise<SyncView> {
    return json(await fetch(`${BASE}/sync`))
  },
  async startSync(): Promise<JobView> {
    return json(await fetch(`${BASE}/sync`, { method: 'POST' }))
  },
  async getSession(id: string): Promise<SessionView> {
    return json(await fetch(`${BASE}/sessions/${id}`))
  },
  async *send(id: string, text: string): AsyncGenerator<InterviewEvent> {
    const res = await fetch(`${BASE}/sessions/${id}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
    yield* readSSE<InterviewEvent>(res)
  },
  async finalize(id: string): Promise<SessionView> {
    return json(await fetch(`${BASE}/sessions/${id}/finalize`, { method: 'POST' }))
  },
  async reopen(id: string): Promise<SessionView> {
    return json(await fetch(`${BASE}/sessions/${id}/reopen`, { method: 'POST' }))
  },
  async chartOptions(jobId?: string | null): Promise<ChartOptions> {
    const q = jobId ? `?job_id=${encodeURIComponent(jobId)}` : ''
    return json(await fetch(`${BASE}/charts/options${q}`))
  },
  async renderChart(req: ChartRequest, jobId?: string | null, signal?: AbortSignal): Promise<ChartResult> {
    const q = jobId ? `?job_id=${encodeURIComponent(jobId)}` : ''
    return json(
      await fetch(`${BASE}/charts/render${q}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
        signal,
      }),
    )
  },
  async sendFeedback(sessionId: string, body: FeedbackIn): Promise<FeedbackView> {
    return json(
      await fetch(`${BASE}/sessions/${sessionId}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    )
  },
  async feedbackCategories(): Promise<FeedbackCategories> {
    return json(await fetch(`${BASE}/feedback/categories`))
  },
  async *jobEvents(jobId: string): AsyncGenerator<JobEvent> {
    yield* readSSE<JobEvent>(await fetch(`${BASE}/jobs/${jobId}/events`))
  },
}

export const STREAM_INTERRUPTED = '進捗の受信が途切れました。再読み込みしてください'

/**
 * ジョブ（生成・取り込み・反映）の進捗を追う。step と done のイベントを当てた JobView を onUpdate に
 * 渡し（最初に initial も渡す）、最後の状態を返す。done が届かずにストリームが終わったら（backend の
 * 再起動・通信の切断など）、実行中のまま固めずに失敗にする（例外はそのまま投げ直す）
 */
export async function trackJob(
  initial: JobView,
  onUpdate: (job: JobView) => void,
  interrupted: string = STREAM_INTERRUPTED,
): Promise<JobView> {
  let job = initial
  onUpdate(job)
  try {
    for await (const ev of api.jobEvents(job.id)) {
      if (ev.event === 'step') {
        const step = ev.data
        job = { ...job, steps: job.steps.map((x) => (x.step === step.step ? step : x)) }
      } else {
        job = ev.data
      }
      onUpdate(job)
    }
  } finally {
    if (job.status === 'running') {
      job = { ...job, status: 'failed', error: interrupted }
      onUpdate(job)
    }
  }
  return job
}
