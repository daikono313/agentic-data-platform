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
 * マージ後の反映（#74、#106）。main に新しい変更（マージされた PR）があれば知らせ、取り込んで
 * dbt build と検査をし、一覧を新しくする。backend が自動で反映を始めたら、その進捗を追い、終われば
 * 一覧を読み直す（画面を開いたままでよい）。手元が main 以外・変更があるときは理由を出す（触らない）。
 */
export function SyncBanner({ onDone, pollMs = SYNC_POLL_MS }: { onDone?: () => void; pollMs?: number }) {
  const [view, setView] = useState<SyncView | null>(null)
  const [job, setJob] = useState<JobView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const following = useRef<string | null>(null)

  useEffect(() => {
    let alive = true
    const check = () =>
      api
        .syncStatus()
        .then((v) => {
          if (!alive) return
          setView(v)
          if (v.job?.status === 'running' && following.current !== v.job.id) follow(v.job)
        })
        .catch(() => alive && setView(null)) // 反映が使えない環境では何も出さない
    check()
    const timer = setInterval(check, pollMs)
    return () => {
      alive = false
      clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollMs])

  const follow = async (initial: JobView) => {
    following.current = initial.id
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
    setError(null)
    setJob(null)
    try {
      await follow(await api.startSync())
    } catch (e) {
      setError(errorText(e))
      // 別のタブで実行中なら、それを表示する
      const now = await api.syncStatus().catch(() => null)
      if (now?.job?.status === 'running') follow(now.job)
    }
  }

  if (!view?.status) return null
  const { status, auto } = view
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
      {status.behind > 0 && !job && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p>
            main に新しい変更があります（{status.behind} 件。マージされた PR など）。取り込むと、新しい指標やモデルが一覧に出ます。
          </p>
          {status.can_pull ? (
            <span className="flex items-center gap-2">
              {auto?.enabled && <span className="text-muted-foreground">まもなく自動で取り込みます</span>}
              <Button size="sm" variant="outline" onClick={start}>
                <RefreshCw className="size-3.5" /> 取り込んで更新する
              </Button>
            </span>
          ) : (
            <span className="text-amber-600">
              {status.reason}
              {auto?.enabled && '（自動では取り込めません）'}
            </span>
          )}
        </div>
      )}
      {status.fetch_error && !job && (
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
