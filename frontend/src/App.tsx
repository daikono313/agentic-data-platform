import { useCallback, useEffect, useReducer, useRef, useState } from 'react'

import { api } from '@/api/client'
import type { ChatMessage, InterviewEvent, JobView, SessionView } from '@/api/types'
import { errorText, type Datasets } from '@/api/datasets'
import { BeamCanvas } from '@/components/BeamCanvas'
import { ChartScreen } from '@/components/ChartScreen'
import { DatasetsPanel } from '@/components/DatasetsPanel'
import { ChatPanel } from '@/components/ChatPanel'
import { EventMatrixView } from '@/components/EventMatrixView'
import { JobProgress } from '@/components/JobProgress'
import { PhaseProgress } from '@/components/PhaseProgress'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import demo from '@/demo/sales_by_store.json'
import type { BeamModel, EventMatrix } from '@/generated/beam'

export interface State {
  session: SessionView | null
  draft: string
  busy: boolean
  error: string | null
}

export type Action =
  | { type: 'session'; session: SessionView }
  | { type: 'user'; text: string }
  | { type: 'event'; event: InterviewEvent }
  | { type: 'job'; job: JobView }
  | { type: 'error'; error: string }
  | { type: 'idle' }

export function reducer(state: State, action: Action): State {
  const s = state.session
  switch (action.type) {
    case 'session':
      return { ...state, session: action.session, error: null }
    case 'error':
      return { ...state, busy: false, error: action.error }
    case 'idle':
      return state.busy ? { ...state, busy: false, draft: '' } : state
    case 'job':
      return s ? { ...state, session: { ...s, job: action.job } } : state
    case 'user':
      if (!s) return state
      return {
        ...state,
        busy: true,
        draft: '',
        error: null,
        session: { ...s, choices: [], question: null, messages: [...s.messages, { role: 'user', text: action.text }] },
      }
    case 'event': {
      if (!s) return state
      const { event } = action
      switch (event.event) {
        case 'text':
          return { ...state, draft: state.draft + event.data.text }
        case 'beam':
          return { ...state, session: { ...s, ...event.data } }
        case 'phase':
          return { ...state, session: { ...s, phase: event.data.phase } }
        case 'choices':
          return { ...state, session: { ...s, choices: event.data.options, question: event.data.question } }
        case 'done': {
          const m = event.data.message
          // 本文が空でも、質問だけの発言は残す（#36）
          const messages: ChatMessage[] = m.text || m.question ? [...s.messages, m] : s.messages
          // 選択肢は発言に記録された最後のものを正とする（同じ選択肢で質問だけ変わった場合も反映する）
          const choices = m.choices?.length ? { choices: m.choices, question: m.question ?? null } : {}
          return {
            ...state,
            busy: false,
            draft: '',
            error: event.data.error ?? null,
            session: { ...s, messages, ...choices },
          }
        }
        default:
          return state
      }
    }
  }
}

function demoSession(): SessionView {
  return {
    id: 'demo',
    phase: 'review',
    beam: demo.beam as unknown as BeamModel,
    matrix: demo.matrix as unknown as EventMatrix,
    issues: demo.issues,
    choices: ['確定します', '修正したい'],
    question: '内容はこれでよいですか？',
    messages: [
      { role: 'user', text: demo.beam.request },
      { role: 'assistant', text: '（デモ）ヒアリング結果をまとめました。問題なければ「確定して実装へ」を押してください。' },
    ],
  }
}

export default function App() {
  const params = new URLSearchParams(window.location.search)
  const chart = params.get('chart')
  if (chart) return <ChartScreen metric={chart} jobId={params.get('job')} />
  return <InterviewApp />
}

function InterviewApp() {
  const isDemo = new URLSearchParams(window.location.search).has('demo')
  const [state, dispatch] = useReducer(reducer, {
    session: isDemo ? demoSession() : null,
    draft: '',
    busy: false,
    error: null,
  })
  const session = state.session

  const send = useCallback(
    async (id: string, text: string) => {
      dispatch({ type: 'user', text })
      try {
        for await (const event of api.send(id, text)) dispatch({ type: 'event', event })
      } catch (e) {
        dispatch({ type: 'error', error: String(e) })
      } finally {
        // done が届かずにストリームが終わっても、入力できる状態に戻す
        dispatch({ type: 'idle' })
      }
    },
    [],
  )

  const start = async (request: string) => {
    try {
      const s = await api.createSession(request)
      dispatch({ type: 'session', session: { ...s, messages: [] } })
      window.history.replaceState(null, '', `?session=${s.id}`)
      await send(s.id, request)
    } catch (e) {
      dispatch({ type: 'error', error: String(e) })
    }
  }

  const followJob = useCallback(async (initial: JobView) => {
    let job = initial
    for await (const ev of api.jobEvents(job.id)) {
      if (ev.event === 'step') {
        const step = ev.data
        job = { ...job, steps: job.steps.map((x) => (x.step === step.step ? step : x)) }
      } else {
        job = ev.data
      }
      dispatch({ type: 'job', job })
    }
    if (job.status === 'running') {
      dispatch({ type: 'job', job: { ...job, status: 'failed', error: '進捗の受信が途切れました。再読み込みしてください' } })
    }
  }, [])

  const finalize = async () => {
    if (!session) return
    try {
      const s = await api.finalize(session.id)
      dispatch({ type: 'session', session: s })
      if (s.job) await followJob(s.job)
    } catch (e) {
      dispatch({ type: 'error', error: String(e) })
    }
  }

  const reopen = async () => {
    if (!session) return
    try {
      dispatch({ type: 'session', session: { ...(await api.reopen(session.id)), job: null } })
    } catch (e) {
      dispatch({ type: 'error', error: String(e) })
    }
  }

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('session')
    if (!id || isDemo) return
    api
      .getSession(id)
      .then((s) => {
        dispatch({ type: 'session', session: s })
        // 生成ジョブの実行中に再読み込みした場合は、進捗の受信を再開する
        if (s.job?.status === 'running') followJob(s.job).catch((e) => dispatch({ type: 'error', error: String(e) }))
      })
      .catch((e) => {
        window.history.replaceState(null, '', '/')
        dispatch({ type: 'error', error: `セッションを読み込めませんでした: ${String(e)}` })
      })
  }, [isDemo, followJob])

  // 開始画面とインタビュー中の参照で 1 回だけ読み込んで共有する
  const sharedDatasets = useDatasets(!isDemo)

  if (!session)
    return (
      <StartScreen
        onStart={start}
        error={state.error}
        datasets={sharedDatasets.datasets}
        datasetsError={sharedDatasets.error}
      />
    )

  const event = session.beam.events?.[0]
  const finalized = session.phase === 'finalized'
  const canFinalize =
    !finalized && !state.busy && session.issues.length === 0 && (session.beam.events?.length ?? 0) > 0
  const jobRunning = session.job?.status === 'running'

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
        <div>
          <h1 className="text-base font-bold">{session.beam.title}</h1>
          <p className="text-xs text-muted-foreground">{session.beam.request}</p>
        </div>
        <PhaseProgress phase={session.phase} />
      </header>
      <main className="grid min-h-0 flex-1 gap-4 p-4 lg:grid-cols-[minmax(320px,2fr)_3fr]">
        <Card className="flex h-[calc(100vh-7rem)] min-h-[420px] flex-col gap-0 py-0">
          <ChatPanel
            messages={session.messages}
            draft={state.draft}
            question={session.question}
            choices={session.choices}
            busy={state.busy}
            disabled={finalized || isDemo}
            onSend={(t) => send(session.id, t)}
          />
        </Card>
        <div className="min-w-0 space-y-4">
          {state.error && <p className="rounded border border-destructive p-2 text-sm text-destructive">{state.error}</p>}
          {!isDemo && (
            <details className="rounded-lg border p-3">
              <summary className="cursor-pointer text-sm font-semibold">使えるデータ（参照用）</summary>
              <div className="mt-3">
                <DatasetsPanel datasets={sharedDatasets.datasets} error={sharedDatasets.error} compact />
              </div>
            </details>
          )}
          <section aria-label="BEAM Canvas">
            <BeamCanvas event={event} />
          </section>
          <Card className="gap-2 py-3">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Event Matrix</CardTitle>
            </CardHeader>
            <CardContent className="px-4">
              <EventMatrixView matrix={session.matrix} />
            </CardContent>
          </Card>
          <Card className="gap-2 py-3">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">{finalized ? '実装' : '確定'}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 px-4">
              {session.job ? (
                <>
                  <JobProgress job={session.job} metrics={session.beam.metrics ?? []} />
                  {!jobRunning && !isDemo && (
                    <Button variant="outline" size="sm" onClick={reopen}>
                      修正して確定し直す
                    </Button>
                  )}
                </>
              ) : (
                <>
                  {session.issues.length > 0 && (
                    <div>
                      <p className="text-xs font-semibold text-muted-foreground">確定までに残っている課題</p>
                      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
                        {session.issues.slice(0, 8).map((i) => (
                          <li key={i}>{i}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <Button onClick={finalize} disabled={!canFinalize || isDemo}>
                    確定して実装へ
                  </Button>
                </>
              )}
            </CardContent>
          </Card>
        </div>
      </main>
    </div>
  )
}

function useDatasets(enabled: boolean) {
  const [datasets, setDatasets] = useState<Datasets | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!enabled) return
    api
      .datasets()
      .then((d) => {
        setDatasets(d)
        setError(null)
      })
      .catch((e) => setError(errorText(e)))
  }, [enabled])
  return { datasets, error }
}

function StartScreen({
  onStart,
  error,
  datasets,
  datasetsError,
}: {
  onStart: (r: string) => void
  error: string | null
  datasets: Datasets | null
  datasetsError: string | null
}) {
  const [request, setRequest] = useState('')
  const input = useRef<HTMLInputElement>(null)
  return (
    <main className="mx-auto grid min-h-screen max-w-5xl gap-8 px-4 py-10 lg:grid-cols-[1fr_1.2fr]">
      <div className="flex flex-col justify-center gap-4">
        <h1 className="text-2xl font-bold">どんな指標を見たいですか？</h1>
        <p className="text-sm text-muted-foreground">
          例:「店舗ごとの売上を見たい」。質問に答えていくと、データモデルと指標を実装した PR ができます。右側で、いま使えるデータを確認できます。
        </p>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (request.trim()) onStart(request.trim())
          }}
        >
          <Input
            name="request"
            aria-label="見たい指標"
            placeholder="xxx の売上指標を取りたい"
            ref={input}
            value={request}
            onChange={(e) => setRequest(e.target.value)}
          />
          <Button type="submit">始める</Button>
        </form>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <a href="?demo" className="text-xs text-muted-foreground underline">
          デモを見る
        </a>
      </div>
      <Card className="max-h-[80vh] gap-2 overflow-y-auto py-4">
        <CardHeader className="px-4">
          <CardTitle className="text-base">使えるデータ</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <DatasetsPanel
            datasets={datasets}
            error={datasetsError}
            onPickExample={(text) => {
              setRequest(text)
              input.current?.focus()
            }}
          />
        </CardContent>
      </Card>
    </main>
  )
}
