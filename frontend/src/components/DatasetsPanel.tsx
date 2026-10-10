import { BarChart3, Database, Table2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { exampleRequests, matches, type Datasets } from '@/api/datasets'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'

interface Props {
  datasets: Datasets | null
  error?: string | null
  onPickExample?: (text: string) => void
  compact?: boolean
}

function Section({ icon, title, count, children }: { icon: React.ReactNode; title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h2 className="flex items-center gap-1.5 text-sm font-semibold">
        {icon}
        {title}
        <span className="text-xs font-normal text-muted-foreground">{count}</span>
      </h2>
      {children}
    </section>
  )
}

export function DatasetsPanel({ datasets, error, onPickExample, compact }: Props) {
  const [query, setQuery] = useState('')
  if (error) return <p className="text-sm text-destructive">使えるデータを読み込めませんでした: {error}</p>
  if (!datasets) return <p className="text-sm text-muted-foreground">使えるデータを読み込んでいます…</p>

  const metrics = datasets.metrics.filter((m) =>
    matches(query, m.label, m.name, m.description, ...m.dimensions.map((d) => d.label)),
  )
  const models = datasets.models.filter((m) => matches(query, m.name, m.description))
  const sources = datasets.sources.filter((s) => matches(query, s.name, s.description, ...s.columns.map((c) => c.description)))
  const examples = onPickExample ? exampleRequests(datasets.sources) : []

  return (
    <div className="space-y-4" aria-label="使えるデータ">
      <Input aria-label="使えるデータを検索" placeholder="検索（例: 売上、店舗、返品）" value={query} onChange={(e) => setQuery(e.target.value)} />
      {datasets.errors.length > 0 && (
        <ul className="text-xs text-muted-foreground">
          {datasets.errors.map((e) => (
            <li key={e}>⚠ {e}</li>
          ))}
        </ul>
      )}

      <Section icon={<BarChart3 className="size-4" />} title="すぐ見られる指標" count={metrics.length}>
        {metrics.length === 0 && <p className="text-xs text-muted-foreground">まだありません。要求すると作れます。</p>}
        <ul className="space-y-2">
          {metrics.map((m) => (
            <li key={m.name} className="rounded-md border p-2" data-testid={`metric-${m.name}`}>
              <p className="text-sm font-medium">{m.label}</p>
              {m.description && <p className="text-xs text-muted-foreground">{m.description}</p>}
              <div className="mt-1 flex flex-wrap gap-1">
                {[...m.dimensions]
                  .sort((a, b) => Number(b.kind === 'time') - Number(a.kind === 'time'))
                  .slice(0, compact ? 6 : 12)
                  .map((d) => (
                    <Badge
                      key={d.name}
                      variant={d.kind === 'time' ? 'outline' : 'secondary'}
                      title={d.description || d.name}
                    >
                      {d.label}
                    </Badge>
                  ))}
                {m.dimensions.length > (compact ? 6 : 12) && (
                  <span className="text-xs text-muted-foreground">ほか {m.dimensions.length - (compact ? 6 : 12)}</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Section>

      {examples.length > 0 && (
        <section className="space-y-1">
          <h2 className="text-sm font-semibold">元データから作れそうな指標の例</h2>
          <div className="flex flex-wrap gap-1">
            {examples.map((e) => (
              <button
                key={e}
                type="button"
                className="rounded-full border px-2 py-0.5 text-xs hover:bg-muted"
                onClick={() => onPickExample?.(e)}
              >
                {e}
              </button>
            ))}
          </div>
        </section>
      )}

      {!compact && (
        <Section icon={<Table2 className="size-4" />} title="整備済みのモデル" count={models.length}>
          <ul className="grid gap-1 sm:grid-cols-2">
            {models.map((m) => (
              <li key={m.name} className="text-xs">
                <span className="font-mono">{m.name}</span>
                <span className="ml-1 text-muted-foreground">{m.description.split('\n')[0]}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section icon={<Database className="size-4" />} title="元データ" count={sources.length}>
        <ul className="space-y-1">
          {sources.map((s) => (
            <SourceItem key={s.name} source={s} />
          ))}
        </ul>
      </Section>
    </div>
  )
}

function SourceItem({ source }: { source: Datasets['sources'][number] }) {
  const [open, setOpen] = useState(false)
  useEffect(() => setOpen(false), [source.name])
  return (
    <li className="text-xs">
      <button type="button" className="text-left hover:underline" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="font-medium">{source.description || source.name}</span>
        <span className="ml-1 text-muted-foreground">
          {source.name}・{source.row_count.toLocaleString()} 行
        </span>
      </button>
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
