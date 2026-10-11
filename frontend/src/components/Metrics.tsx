import { ArrowLeft, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { chartUrl } from '@/api/charts'
import { errorText, filterMetrics, groupDimensions, type BusMatrix, type Datasets, type MetricInfo } from '@/api/datasets'
import { Input } from '@/components/ui/input'

/** 指標 1 件: 説明・切り口（dimension ごと）・チャートで見る（#107） */
function MetricCard({ metric, bus }: { metric: MetricInfo; bus?: BusMatrix | null }) {
  const groups = groupDimensions(metric.dimensions, bus)
  return (
    <li className="space-y-1.5 rounded-md border p-3" data-testid={`metric-${metric.name}`}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium">
          {metric.label}
          <span className="ml-1 font-mono text-xs font-normal text-muted-foreground">{metric.name}</span>
        </p>
        <a href={chartUrl(metric.name)} className="shrink-0 text-xs text-primary underline">
          チャートで見る
        </a>
      </div>
      {metric.description && <p className="text-xs text-muted-foreground">{metric.description}</p>}
      {groups.length > 0 && (
        <div className="space-y-0.5 text-xs">
          <p className="font-medium">切り口</p>
          {groups.map((g) => (
            <div
              key={g.key}
              role="group"
              aria-label={g.label}
              className="grid grid-cols-[8rem_1fr] gap-x-3"
              data-testid={`dimension-group-${g.key}`}
            >
              <span className="text-muted-foreground">{g.label}</span>
              <span>{g.dimensions.map((d) => d.label).join('・')}</span>
            </div>
          ))}
        </div>
      )}
    </li>
  )
}

/** 指標の一覧のページ（`?metrics`。`q` は検索語の初期値。#107） */
export function MetricsScreen() {
  const [datasets, setDatasets] = useState<Datasets | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState(() => (new URLSearchParams(window.location.search).get('q') ?? '').slice(0, 200))
  useEffect(() => {
    api
      .datasets()
      .then(setDatasets)
      .catch((e) => setError(errorText(e)))
  }, [])

  const metrics = datasets ? filterMetrics(datasets.metrics, query, datasets.bus) : []
  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="size-4" /> ホーム
      </a>
      <div className="flex items-baseline gap-2">
        <h1 className="text-xl font-bold">指標の一覧</h1>
        {datasets && (
          <span className="text-sm text-muted-foreground" data-testid="metrics-count">
            {metrics.length} / {datasets.metrics.length} 件
          </span>
        )}
      </div>
      {error && <p className="text-sm text-destructive">指標を読み込めませんでした: {error}</p>}
      {!datasets && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> 読み込んでいます…
        </p>
      )}
      {datasets && (
        <>
          {datasets.errors.length > 0 && (
            <ul className="text-xs text-muted-foreground">
              {datasets.errors.map((e) => (
                <li key={e}>⚠ {e}</li>
              ))}
            </ul>
          )}
          <Input
            aria-label="指標を検索"
            placeholder="指標を絞り込む（例: 売上、店舗、返品）"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="max-w-sm"
          />
          {metrics.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {datasets.metrics.length === 0 ? 'まだありません。要求すると作れます。' : '検索に合う指標はありません。'}
            </p>
          ) : (
            <ul className="grid gap-2 md:grid-cols-2">
              {metrics.map((m) => (
                <MetricCard key={m.name} metric={m} bus={datasets.bus} />
              ))}
            </ul>
          )}
        </>
      )}
    </main>
  )
}
