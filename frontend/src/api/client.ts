import type { ChartOptions, ChartRequest, ChartResult } from './charts'
import type { CleansingView, Datasets, OnboardingDraft, OntologyPatch, OntologyView, SourceProfile } from './datasets'
import type { FeedbackCategories, FeedbackIn, FeedbackView } from './feedback'
import type { InterviewEvent, JobEvent, JobView, SessionView } from './types'

const BASE = '/api'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text()
    throw new Error(body || `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

/** text/event-stream を読み、`event:` と `data:`（JSON）の組を順に返す */
export async function* readSSE<T extends { event: string; data: unknown }>(
  res: Response,
): AsyncGenerator<T> {
  if (!res.ok || !res.body) throw new Error(await res.text())
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
  async startOnboarding(table: string, patch: OntologyPatch, cleansing: string, redo = false): Promise<JobView> {
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
