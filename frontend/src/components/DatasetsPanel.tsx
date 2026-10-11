import { BarChart3, Boxes, Database, Table2 } from 'lucide-react'
import { useState } from 'react'

import { chartUrl } from '@/api/charts'
import { filterMetrics, groupDimensions, matches, METRICS_URL, onboardUrl, sourceUrl, type Datasets } from '@/api/datasets'
import { BusMatrixView, filterBus, type BusMetric } from '@/components/BusMatrix'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'

interface Props {
  datasets: Datasets | null
  error?: string | null
  /** インタビュー中の参照用（狭い枠）。リンクは別タブで開く */
  compact?: boolean
  /** バスマトリクスの空欄から要求を入れる（開始画面だけ。#70） */
  onRequest?: (text: string) => void
}

function Section({
  icon,
  title,
  count,
  note,
  children,
}: {
  icon: React.ReactNode
  title: string
  count: number
  note?: string
  children: React.ReactNode
}) {
  return (
    <section className="space-y-2" aria-label={title}>
      <h3 className="flex items-baseline gap-1.5 text-sm font-semibold">
        <span className="self-center">{icon}</span>
        {title}
        <span className="text-xs font-normal text-muted-foreground">{count}</span>
        {note && <span className="text-xs font-normal text-muted-foreground">— {note}</span>}
      </h3>
      {children}
    </section>
  )
}

/**
 * 使えるデータの一覧（#38、#62）。
 * 上から「既存のデータ（指標 → fact × dimension のバスマトリクス）」「使える源泉」の順に並べる。
 * バスマトリクスが無い（semantic manifest が無い）ときは fact と dimension の一覧を出す。
 */
export function DatasetsPanel({ datasets, error, compact, onRequest }: Props) {
  const [query, setQuery] = useState('')
  if (error) return <p className="text-sm text-destructive">使えるデータを読み込めませんでした: {error}</p>
  if (!datasets) return <p className="text-sm text-muted-foreground">使えるデータを読み込んでいます…</p>

  const metrics = filterMetrics(datasets.metrics, query)
  const models = datasets.models.filter((m) =>
    matches(query, m.name, m.description, ...m.columns.map((c) => c.description)),
  )
  const facts = models.filter((m) => m.kind === 'fct')
  const dims = models.filter((m) => m.kind !== 'fct')
  // バスマトリクスがあれば fact / dimension の一覧の代わりに出す。無ければ一覧（#62）
  const bus = datasets.bus && datasets.bus.rows.length > 0 ? filterBus(datasets.bus, query, datasets.models) : null
  const inBus = new Set([...(datasets.bus?.groups.map((g) => g.name) ?? []), ...(datasets.bus?.rows.map((r) => r.name) ?? [])])
  const unlisted = models.filter((m) => !inBus.has(m.name))
  // fct ごとの指標（バスマトリクスの ✓ から、その切り口のチャートへ）
  const metricsByFact = new Map<string, BusMetric[]>()
  // 単純な指標を先に（比率などは分母の fact の切り口に限られる）
  const ordered = [...datasets.metrics].sort((a, b) => Number(a.type !== 'simple') - Number(b.type !== 'simple'))
  for (const m of ordered) {
    if (!m.model) continue
    const dims = m.dimensions.filter((d) => d.kind !== 'time').map((d) => d.name)
    metricsByFact.set(m.model, [...(metricsByFact.get(m.model) ?? []), { name: m.name, label: m.label, dimensions: dims }])
  }
  const sources = datasets.sources.filter((s) => matches(query, s.name, s.description, ...s.columns.map((c) => c.description)))
  // 開始画面には代表の数件だけ。すべては指標の一覧のページで（#107）
  const maxMetrics = compact ? 3 : 4
  const linkProps: React.AnchorHTMLAttributes<HTMLAnchorElement> = compact ? { target: '_blank', rel: 'noreferrer' } : {}

  return (
    <section className="space-y-6" aria-label="使えるデータ">
      <Input
        aria-label="使えるデータを検索"
        placeholder="データを絞り込む（例: 売上、店舗、返品）"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className={compact ? '' : 'max-w-sm'}
      />
      {datasets.errors.length > 0 && (
        <ul className="text-xs text-muted-foreground">
          {datasets.errors.map((e) => (
            <li key={e}>⚠ {e}</li>
          ))}
        </ul>
      )}

      <div className="space-y-4">
        <h2 className="flex items-baseline justify-between border-b pb-1 text-base font-bold">
          既存のデータ
          <a href="?ontology" className="text-xs font-normal text-primary underline" {...linkProps}>
            オントロジー（概念の関係）を見る
          </a>
        </h2>
        <Section icon={<BarChart3 className="size-4" />} title="指標（metrics）" count={metrics.length} note="すぐチャートで見られる">
          {metrics.length === 0 && <p className="text-xs text-muted-foreground">まだありません。要求すると作れます。</p>}
          <ul className={compact ? 'space-y-1' : 'grid gap-x-4 gap-y-1 md:grid-cols-2'}>
            {metrics.slice(0, maxMetrics).map((m) => (
              <li key={m.name} className="flex items-baseline justify-between gap-2 text-xs" data-testid={`metric-${m.name}`}>
                <span className="min-w-0">
                  <span className="text-sm font-medium">{m.label}</span>
                  <span className="ml-1 text-muted-foreground">
                    {groupDimensions(m.dimensions, datasets.bus)
                      .map((g) => g.label)
                      .join('・')}
                  </span>
                </span>
                <a href={chartUrl(m.name)} className="shrink-0 text-primary underline" {...linkProps}>
                  チャートで見る
                </a>
              </li>
            ))}
          </ul>
          {datasets.metrics.length > 0 && (
            <a href={METRICS_URL} className="inline-block text-xs text-primary underline" {...linkProps}>
              {metrics.length > maxMetrics
                ? `ほか ${metrics.length - maxMetrics} 件。すべての指標（説明・切り口）を見る`
                : 'すべての指標（説明・切り口）を見る'}
            </a>
          )}
        </Section>

        {bus ? (
          <Section
            icon={<Table2 className="size-4" />}
            title="fact × dimension"
            count={bus.rows.length}
            note="バスマトリクス。✓ の dimension の属性で fact を集計できる"
          >
            <BusMatrixView
              bus={bus}
              models={datasets.models}
              compact={compact}
              metrics={metricsByFact}
              onRequest={onRequest}
            />
            {unlisted.length > 0 && (
              <div className="space-y-1 pt-2">
                <p className="text-xs text-muted-foreground">Semantic Layer に未登録のモデル（バスマトリクスに出ない）</p>
                <ModelList models={unlisted} />
              </div>
            )}
          </Section>
        ) : (
          <div className={compact ? 'space-y-4' : 'grid gap-4 lg:grid-cols-2'}>
            <Section icon={<Table2 className="size-4" />} title="fact" count={facts.length} note="出来事の記録">
              <ModelList models={facts} />
            </Section>
            <Section icon={<Boxes className="size-4" />} title="dimension" count={dims.length} note="切り口の属性">
              <ModelList models={dims} />
            </Section>
          </div>
        )}
      </div>

      <div className="space-y-2">
        <h2 className="border-b pb-1 text-base font-bold">
          使える源泉（元データ）
          <span className="ml-1.5 text-xs font-normal text-muted-foreground">{sources.length}</span>
        </h2>
        <SourceCoverage sources={sources} />
        <ul className={compact ? 'divide-y' : 'grid divide-y md:grid-cols-2 md:gap-x-6'}>
          {sources.map((s) => (
            <SourceItem key={s.name} source={s} linkProps={linkProps} />
          ))}
        </ul>
      </div>
    </section>
  )
}

function ModelList({ models }: { models: Datasets['models'] }) {
  if (models.length === 0) return <p className="text-xs text-muted-foreground">まだありません。</p>
  return (
    <ul className="space-y-1">
      {models.map((m) => (
        <ModelItem key={m.name} model={m} />
      ))}
    </ul>
  )
}

function SourceItem({
  source,
  linkProps,
}: {
  source: Datasets['sources'][number]
  linkProps: React.AnchorHTMLAttributes<HTMLAnchorElement>
}) {
  const [open, setOpen] = useState(false)
  return (
    <li className="py-1.5 text-xs" data-testid={`source-${source.name}`}>
      <div className="flex items-start justify-between gap-2">
        <button type="button" className="min-w-0 text-left hover:underline" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="block font-medium">{source.description.split('\n')[0] || source.name}</span>
          <span className="block text-muted-foreground">
            <span className="font-mono">{source.name}</span>・{source.row_count.toLocaleString()} 行・{source.columns.length} 列
          </span>
          <span className="mt-0.5 flex flex-wrap gap-1">
            {(source.used_by ?? []).map((m) => (
              <Badge key={m} variant="secondary" className="font-mono text-[10px]">
                {m}
              </Badge>
            ))}
            {!source.unmapped && Array.isArray(source.used_by) && source.used_by.length === 0 && (
              <Badge variant="outline" className="text-[10px] text-muted-foreground">
                未活用（fact / dim に使われていない）
              </Badge>
            )}
          </span>
        </button>
        <span className="flex shrink-0 flex-col items-end gap-0.5">
          <a href={sourceUrl(source.name)} className="text-primary underline" {...linkProps}>
            <Database className="mr-0.5 inline size-3" />
            プロファイル
          </a>
          {source.unmapped && (
            <a href={onboardUrl(source.name)} className="text-primary underline" {...linkProps}>
              <Badge variant="outline" className="mr-1 border-amber-500 text-amber-600">
                未整理
              </Badge>
              取り込む
            </a>
          )}
        </span>
      </div>
      {open && (
        <ul className="mt-1 ml-3 list-disc space-y-0.5 pl-3 text-muted-foreground">
          {source.columns.map((c) => (
            <li key={c.name}>
              <span className="font-mono">{c.name}</span> {c.description}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function ModelItem({ model }: { model: Datasets['models'][number] }) {
  const [open, setOpen] = useState(false)
  const summary = (
    <>
      <span className="font-mono font-medium">{model.name}</span>
      <span className="ml-1 text-muted-foreground">
        {model.description.split('\n')[0]}
        {model.columns.length > 0 && `（${model.columns.length} 列）`}
      </span>
    </>
  )
  if (model.columns.length === 0) return <li className="text-xs">{summary}</li>
  return (
    <li className="text-xs">
      <button type="button" className="text-left hover:underline" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {summary}
      </button>
      {open && (
        <ul className="mt-1 ml-3 list-disc space-y-0.5 pl-3 text-muted-foreground">
          {model.columns.map((c) => (
            <li key={c.name}>
              <span className="font-mono">{c.name}</span> {c.description}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** 源泉の活用状況の要約（#72）: fact / dim に使われている・未活用・未整理（オントロジーに無い）の数 */
function SourceCoverage({ sources }: { sources: Datasets['sources'] }) {
  // dbt の manifest が無いと、使われているかは分からない（未活用とは言わない）
  if (sources.some((s) => s.used_by === null || s.used_by === undefined)) return null
  const unmapped = sources.filter((s) => s.unmapped).length
  const used = sources.filter((s) => !s.unmapped && (s.used_by ?? []).length > 0).length
  const unused = sources.length - used - unmapped
  return (
    <p className="text-xs text-muted-foreground" data-testid="source-coverage">
      fact / dim に使われている {used}・未活用 {unused}
      {unmapped > 0 && `・未整理（オントロジーに無い） ${unmapped}`}
    </p>
  )
}
