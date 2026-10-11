import { RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import { errorText } from '@/api/datasets'
import type { JobView, SyncView } from '@/api/types'
import { JobProgress } from '@/components/JobProgress'
import { Button } from '@/components/ui/button'

/** 状態を確かめ直す間隔（backend の自動反映が始めたジョブに気づくため。#106） */
export const SYNC_POLL_MS = 15_000

/**
 * 結果を見た反映ジョブの id。別の画面（インタビュー・履歴）はページを読み直して開くので、
 * ブラウザに残す。ポーリングの間や別の画面を開いている間に終わった反映も、開始画面に戻ったときに
 * 結果を出し、一覧を読み直すため（#106）
 */
const SEEN_KEY = 'adp.sync.seenJob'
function loadSeen(): string | null {
  try {
    return window.localStorage.getItem(SEEN_KEY)
  } catch {
    return null
  }
}
function saveSeen(id: string) {
  try {
    window.localStorage.setItem(SEEN_KEY, id)
  } catch {
    // 残せなくても、この画面の間は ref で覚えている
  }
}

/**
 * マージ後の反映（#74、#106）。main に新しい変更（マージされた PR）があれば知らせ、取り込んで
 * dbt build と検査をし、一覧を新しくする。backend が自動で反映を始めたら、その進捗を追い、終われば
 * 一覧を読み直す（画面を開いたままでよい）。手元が main 以外・変更があるときは理由を出す（触らない）。
 */
export function SyncBanner({ onDone, pollMs = SYNC_POLL_MS }: { onDone?: () => void; pollMs?: number }) {
  const [view, setView] = useState<SyncView | null>(null)
  const [job, setJob] = useState<JobView | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** 進捗を追っている（または始めようとしている）ジョブ。追うのは一度に 1 つだけ */
  const following = useRef<string | null>(null)
  const seen = useRef<string | null>(loadSeen())
  /** この画面で追ったジョブ（追い終わった後に、前に送った問い合わせの「実行中」が届いても追い直さない） */
  const followed = useRef(new Set<string>())
  const markSeen = (id: string) => {
    seen.current = id
    saveSeen(id)
  }

  useEffect(() => {
    let alive = true
    let sent = 0
    let applied = 0
    const check = () => {
      if (document.visibilityState === 'hidden') return // 見ていないタブでは問い合わせない
      const seq = ++sent
      api
        .syncStatus()
        .then((v) => {
          if (!alive || seq < applied) return // 後から送った問い合わせの結果を、古い結果で上書きしない
          applied = seq
          setView(v)
          const j = v.job
          if (!j || following.current) return
          if (j.status === 'running') {
            if (!followed.current.has(j.id)) follow(j)
          } else if (j.id !== seen.current) {
            // 前回の問い合わせの後に始まって終わった反映（自動・別のタブ・別の画面を開いている間）
            const first = seen.current === null // このブラウザで初めて開いた: 古い成功は出さない
            markSeen(j.id)
            if (!first || j.status === 'failed') setJob(j)
            if (!first && j.status === 'succeeded') onDone?.()
          }
        })
        .catch(() => {
          // 反映が使えない環境では何も出さない。反映を追っている間の一時的な失敗では消さない
          if (alive && !following.current) setView(null)
        })
    }
    check()
    const timer = setInterval(check, pollMs)
    document.addEventListener('visibilitychange', check)
    return () => {
      alive = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', check)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollMs])

  const follow = async (initial: JobView) => {
    following.current = initial.id
    followed.current.add(initial.id)
    markSeen(initial.id) // 終わった後の問い合わせで、同じジョブの結果を出し直さない
    let current = initial
    setJob(current)
    try {
      for await (const ev of api.jobEvents(current.id)) {
        if (ev.event === 'step') {
          const s = ev.data
          current = { ...current, steps: current.steps.map((x) => (x.step === s.step ? s : x)) }
        } else {
          current = ev.data
        }
        setJob(current)
      }
      if (current.status === 'running') {
        // 進捗のストリームが途中で切れた（backend の再起動など）。実行中のまま固めない
        current = { ...current, status: 'failed', error: '進捗を受け取れなくなりました。もう一度実行してください' }
        setJob(current)
      }
      if (current.status === 'succeeded') onDone?.()
    } catch (e) {
      setError(errorText(e))
    } finally {
      following.current = null
    }
  }

  const start = async () => {
    if (following.current) return
    setError(null)
    setJob(null)
    // 始める前に印を付ける（応答を待つ間の問い合わせが、同じジョブを別に追わないように）
    following.current = 'starting'
    let started: JobView
    try {
      started = await api.startSync()
    } catch (e) {
      following.current = null
      setError(errorText(e))
      // 別のタブ・自動反映で実行中なら、それを表示する
      const now = await api.syncStatus().catch(() => null)
      if (now?.job?.status === 'running' && !following.current) follow(now.job)
      return
    }
    await follow(started)
  }

  if (!view?.status) return null
  const { status, auto } = view
  const running = job?.status === 'running'
  if (status.behind === 0 && !job) {
    // 手で取り込んだ後など、dbt だけ作り直したいとき
    return status.can_rebuild ? (
      <p className="text-right text-xs">
        <button type="button" className="text-muted-foreground underline" onClick={start}>
          dbt を作り直して一覧を更新する
        </button>
        {error && <span className="ml-2 text-destructive">{error}</span>}
      </p>
    ) : null
  }
  return (
    <div className="space-y-2 rounded-md border border-sky-500/50 p-3 text-xs" data-testid="sync-banner">
      {status.behind > 0 && !running && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p>
            main に新しい変更があります（{status.behind} 件。マージされた PR など）。取り込むと、新しい指標やモデルが一覧に出ます。
          </p>
          {status.can_pull ? (
            <span className="flex items-center gap-2">
              {auto?.enabled && (
                <span className="text-muted-foreground">{auto.last_note ?? 'まもなく自動で取り込みます'}</span>
              )}
              {job?.status !== 'failed' && ( // 失敗の後は下の「もう一度実行する」
                <Button size="sm" variant="outline" onClick={start}>
                  <RefreshCw className="size-3.5" /> 取り込んで更新する
                </Button>
              )}
            </span>
          ) : (
            <span className="text-amber-600">
              {status.reason}
              {auto?.enabled && '（自動では取り込めません）'}
            </span>
          )}
        </div>
      )}
      {status.fetch_error && !running && (
        <p className="text-muted-foreground">origin の最新を取れませんでした（前回の情報です）: {status.fetch_error}</p>
      )}
      {job && <JobProgress job={job} />}
      {job?.status === 'failed' && (
        <Button size="sm" variant="outline" onClick={start}>
          もう一度実行する
        </Button>
      )}
      {job?.status === 'succeeded' && <p className="text-green-700">反映しました。</p>}
      {error && <p className="text-destructive">{error}</p>}
    </div>
  )
}
