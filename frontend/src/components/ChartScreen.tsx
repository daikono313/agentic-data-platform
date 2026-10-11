import { ArrowLeft, Loader2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { GRAINS, svgDataUrl, type ChartOptions, type ChartResult, type Grain } from '@/api/charts'
import { api } from '@/api/client'
import { errorText, rankDimensions } from '@/api/datasets'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

const SELECT = 'h-9 rounded-md border bg-background px-2 text-sm'

/** 指標を選び、切り口・粒度・期間を変えて見る（#43）。jobId があれば生成 PR の内容（マージ前）で見る */
export function ChartScreen({
  metric: initial,
  jobId,
  dimension: initialDimension = null,
}: {
  metric: string
  jobId?: string | null
  /** 最初に選ぶ切り口（バスマトリクスの ✓ から開いたとき。#70） */
  dimension?: string | null
}) {
  const [options, setOptions] = useState<ChartOptions | null>(null)
  const [metric, setMetric] = useState(initial)
  const [dimension, setDimension] = useState<string | null>(initialDimension)
  const [grain, setGrain] = useState<Grain | null>('month')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [result, setResult] = useState<ChartResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .chartOptions(jobId)
      .then(setOptions)
      .catch((e) => setError(errorText(e)))
  }, [jobId])

  const current = options?.metrics.find((m) => m.name === metric)
  const dimensions = useMemo(
    () => rankDimensions((current?.dimensions ?? []).filter((d) => d.kind === 'categorical')),
    [current],
  )

  // 指標に無い切り口が指定されていたら、切り口なしにする
  useEffect(() => {
    if (current && dimension && !dimensions.some((d) => d.name === dimension)) setDimension(null)
  }, [current, dimension, dimensions])

  useEffect(() => {
    if (!current) return
    if (dimension && !dimensions.some((d) => d.name === dimension)) return
    const controller = new AbortController()
    // 入力のたびに描画しないよう、少し待ってから描画する
    const timer = setTimeout(() => {
      setLoading(true)
      setError(null)
      api
        .renderChart(
          { metric, dimension, grain, start: start || null, end: end || null },
          jobId,
          controller.signal,
        )
        .then((r) => {
          // 新しい条件で描き直し始めていたら、古い結果は表示しない
          if (!controller.signal.aborted) setResult(r)
        })
        .catch((e) => {
          if (controller.signal.aborted) return
          // 失敗したら前のチャートを消す（選んだ条件と違うチャートを残さない）
          setResult(null)
          setError(errorText(e))
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false)
        })
    }, 300)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [current, metric, dimension, dimensions, grain, start, end, jobId])

  const notFound = options && !current

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <div className="flex flex-wrap items-center gap-2">
        <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
          <ArrowLeft className="size-4" /> ホーム
        </a>
        <h1 className="text-lg font-bold">{current?.label ?? metric}</h1>
        {options?.preview && <Badge variant="outline">生成 PR の内容でプレビュー（マージ前）</Badge>}
      </div>
      {current?.description && <p className="text-sm text-muted-foreground">{current.description}</p>}

      <Card className="gap-2 py-3">
        <CardContent className="flex flex-wrap items-end gap-3 px-4">
          <label className="grid gap-1 text-xs">
            指標
            <select
              aria-label="指標"
              className={SELECT}
              value={metric}
              onChange={(e) => {
                setMetric(e.target.value)
                setDimension(null)
              }}
            >
              {notFound && <option value={metric}>{metric}</option>}
              {options?.metrics.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-xs">
            切り口
            <select
              aria-label="切り口"
              className={SELECT}
              value={dimension ?? ''}
              onChange={(e) => setDimension(e.target.value || null)}
            >
              <option value="">なし</option>
              {dimensions.map((d) => (
                <option key={d.name} value={d.name}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-xs">
            粒度
            <select
              aria-label="粒度"
              className={SELECT}
              value={grain ?? ''}
              onChange={(e) => setGrain((e.target.value || null) as Grain | null)}
            >
              {GRAINS.map((g) => (
                <option key={g.label} value={g.value ?? ''}>
                  {g.label}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-xs">
            期間（開始）
            <input aria-label="期間の開始" type="date" className={SELECT} value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="grid gap-1 text-xs">
            期間（終了）
            <input aria-label="期間の終了" type="date" className={SELECT} value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
        </CardContent>
      </Card>

      {options?.errors.map((e) => (
        <p key={e} className="text-xs text-muted-foreground">
          ⚠ {e}
        </p>
      ))}
      {notFound && (
        <p className="text-sm text-destructive">
          指標 {metric} がありません。{options?.preview ? '' : '生成 PR をマージして make dbt-build を実行すると表示できます。'}
        </p>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}

      <Card className="gap-2 py-3">
        <CardHeader className="flex flex-row items-center gap-2 px-4">
          <CardTitle className="text-sm">{result?.title ?? 'チャート'}</CardTitle>
          {loading && <Loader2 className="size-4 animate-spin" aria-label="描画中" />}
        </CardHeader>
        <CardContent className="px-4">
          {result ? (
            <img src={svgDataUrl(result.svg)} alt={result.title} className="w-full" />
          ) : (
            !error && !notFound && <p className="text-sm text-muted-foreground">描画しています…</p>
          )}
          {result && (
            <details className="mt-2 text-xs">
              <summary className="cursor-pointer text-muted-foreground">SQL（MetricFlow が作成）</summary>
              <pre className="mt-1 overflow-x-auto rounded bg-muted p-2">{result.sql}</pre>
            </details>
          )}
        </CardContent>
      </Card>
    </main>
  )
}
