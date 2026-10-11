import { ArrowLeft, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { chartUrl } from '@/api/charts'
import { errorText, filterMetrics, groupDimensions, type BusMatrix, type Datasets, type MetricInfo } from '@/api/datasets'
import { Input } from '@/components/ui/input'

/** 指標 1 件: 説明・切り口（dimension ごと）・チャートで見る（#107） */
export function MetricCard({ metric, bus }: { metric: MetricInfo; bus?: BusMatrix | null }) {
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
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs" aria-label="切り口">
          {groups.map((g) => (
            <div key={g.key} className="contents" data-testid={`dimension-group-${g.key}`}>
              <dt className="font-medium whitespace-nowrap text-muted-foreground">{g.label}</dt>
              <dd>{g.dimensions.map((d) => d.label).join('・')}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  )
}

/** 指標の一覧のページ（`?metrics`。#107） */
export function MetricsScreen() {
  const [datasets, setDatasets] = useState<Datasets | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  useEffect(() => {
    api
      .datasets()
      .then(setDatasets)
      .catch((e) => setError(errorText(e)))
  }, [])

  const metrics = datasets ? filterMetrics(datasets.metrics, query) : []
  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="size-4" /> ホーム
      </a>
      <h1 className="text-xl font-bold">
        指標の一覧
        {datasets && <span className="ml-2 text-sm font-normal text-muted-foreground">{datasets.metrics.length} 件</span>}
      </h1>
      {error && <p className="text-sm text-destructive">指標を読み込めませんでした: {error}</p>}
      {!datasets && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> 読み込んでいます…
        </p>
      )}
      {datasets && (
        <>
          <Input
            aria-label="指標を検索"
            placeholder="指標を絞り込む（例: 売上、店舗、返品）"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="max-w-sm"
          />
          {metrics.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {datasets.metrics.length === 0 ? 'まだありません。要求すると作れます。' : '条件に合う指標がありません。'}
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
