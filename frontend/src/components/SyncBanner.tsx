import { RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { errorText } from '@/api/datasets'
import type { JobView, SyncView } from '@/api/types'
import { JobProgress } from '@/components/JobProgress'
import { Button } from '@/components/ui/button'

/**
 * マージ後の反映（#74）。main に新しい変更（マージされた PR）があれば知らせ、取り込んで dbt build
 * と検査をし、一覧を新しくする。手元が main 以外・変更があるときは理由を出す（触らない）。
 */
export function SyncBanner({ onDone }: { onDone?: () => void }) {
  const [view, setView] = useState<SyncView | null>(null)
  const [job, setJob] = useState<JobView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .syncStatus()
      .then((v) => {
        setView(v)
        if (v.job?.status === 'running') follow(v.job)
      })
      .catch(() => setView(null)) // 反映が使えない環境では何も出さない
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const follow = async (initial: JobView) => {
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
      if (current.status === 'succeeded') onDone?.()
    } catch (e) {
      setError(errorText(e))
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
  const { status } = view
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
            <Button size="sm" variant="outline" onClick={start}>
              <RefreshCw className="size-3.5" /> 取り込んで更新する
            </Button>
          ) : (
            <span className="text-amber-600">{status.reason}</span>
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
