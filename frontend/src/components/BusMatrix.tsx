import { Check } from 'lucide-react'

import { chartUrl } from '@/api/charts'
import { matches, type BusGroup, type BusMatrix, type ModelInfo } from '@/api/datasets'
import { cn } from '@/lib/utils'

/**
 * 検索語で絞り込む。fact か列のまとまり（dim の名前・属性）が一致したら、それと結合できる相手も残す。
 */
/** 縮退ディメンションの見出しの説明（#148） */
const DEGENERATE_NOTE =
  'dim テーブルを持たず、fact の列にそのまま置いた切り口（区分・番号・日時）。日付で切るときは日付（dim_date）を使う'

export function filterBus(bus: BusMatrix, query: string, models: ModelInfo[]): BusMatrix {
  if (!query.trim()) return bus
  const description = new Map(models.map((m) => [m.name, m.description]))
  const groupHit = new Set(
    bus.groups
      .filter((g) => matches(query, g.name, g.label, description.get(g.name) ?? '', ...g.attributes.map((a) => a.label)))
      .map((g) => g.name),
  )
  const rows = bus.rows.filter(
    (r) => matches(query, r.name, r.label, description.get(r.name) ?? '') || r.groups.some((g) => groupHit.has(g)),
  )
  const rowHit = new Set(rows.filter((r) => matches(query, r.name, r.label, description.get(r.name) ?? '')).map((r) => r.name))
  const groups = bus.groups.filter(
    (g) => groupHit.has(g.name) || rows.some((r) => rowHit.has(r.name) && r.groups.includes(g.name)),
  )
  return { groups, rows }
}

/**
 * Kimball のバスマトリクス（#62）。行が fact、列が dimension（とその属性）。
 * fact が結合できる dimension の属性すべてに ✓ を付ける。fact 自身の属性（退化ディメンション）は最後の列。
 * compact（インタビュー中の狭い枠）では、属性を省いて dimension ごとに 1 列にする。
 */
export interface BusMetric {
  name: string
  label: string
  /** 指標の切り口（カテゴリの切り口の名前）。✓ のリンクは、この属性を切り口に持つ指標だけ */
  dimensions: string[]
}

/**
 * metrics: fct ごとの指標（あれば ✓ をクリックしてその切り口でチャートを開く。#70）
 * onRequest: 空欄（結合できない組み合わせ）をクリックしたときの要求の文（開始画面で入力欄に入れる）
 */
export function BusMatrixView({
  bus,
  models,
  compact,
  metrics,
  onRequest,
}: {
  bus: BusMatrix
  models: ModelInfo[]
  compact?: boolean
  metrics?: Map<string, BusMetric[]>
  onRequest?: (text: string) => void
}) {
  if (bus.rows.length === 0) return <p className="text-xs text-muted-foreground">該当する fact がありません。</p>
  const description = new Map(models.map((m) => [m.name, m.description]))
  // 列のまとまりごとに背景を交互に変える
  const shade = (i: number) => (i % 2 === 0 ? 'bg-muted' : 'bg-background')
  const groupTitle = (g: BusGroup) =>
    g.kind === 'dim' ? g.label : `縮退ディメンション（${g.label}）`
  const columns = (g: BusGroup) => (compact ? [{ name: g.name, label: groupTitle(g) }] : g.attributes)
  return (
    <div className="overflow-x-auto">
      <table className="border-collapse text-xs" aria-label="バスマトリクス">
        <thead>
          <tr>
            <th
              rowSpan={compact ? 1 : 2}
              scope="col"
              className="sticky left-0 z-10 min-w-36 border bg-background px-2 text-left align-bottom font-normal text-muted-foreground"
            >
              fact ＼ dimension
            </th>
            {compact
              ? bus.groups.map((g, i) => (
                  <th
                    key={g.name}
                    scope="col"
                    title={`${g.name}: ${g.attributes.map((a) => a.label).join('、')}`}
                    className={cn('h-28 w-8 min-w-8 max-w-8 border px-0 py-1 align-bottom font-semibold', shade(i))}
                  >
                    <span className="mx-auto block max-h-26 overflow-hidden [writing-mode:vertical-rl]">{groupTitle(g)}</span>
                  </th>
                ))
              : bus.groups.map((g, i) => (
                  <th
                    key={g.name}
                    colSpan={g.attributes.length}
                    scope="colgroup"
                    title={
                      g.kind === 'dim'
                        ? `${g.name}${description.get(g.name) ? `: ${description.get(g.name)}` : ''}${g.sources?.length ? `（元の源泉: ${g.sources.join(', ')}）` : ''}`
                        : `${g.name}: ${DEGENERATE_NOTE}`
                    }
                    className={cn('border px-1 py-1 text-center font-semibold break-all', shade(i))}
                  >
                    {g.kind === 'dim' ? g.label : '縮退ディメンション'}
                    {g.candidate && <span className="ml-1 text-[10px] font-normal text-primary">（候補）</span>}
                    <span className="block font-mono text-[10px] font-normal text-muted-foreground">{g.name}</span>
                  </th>
                ))}
          </tr>
          {!compact && (
            <tr>
              {bus.groups.flatMap((g, i) =>
                g.attributes.map((a) => (
                  <th
                    key={`${g.name}:${a.name}`}
                    scope="col"
                    title={`${a.label}（${a.name}）`}
                    className={cn('h-28 w-8 min-w-8 max-w-8 border px-0 py-1 align-bottom font-normal', shade(i))}
                  >
                    <span className="mx-auto block max-h-26 overflow-hidden [writing-mode:vertical-rl]">{a.label}</span>
                  </th>
                )),
              )}
            </tr>
          )}
        </thead>
        <tbody>
          {bus.rows.map((r) => (
            <tr
              key={r.name}
              data-testid={`bus-${r.name}`}
              className={r.candidate ? 'outline-1 -outline-offset-1 outline-primary outline-dashed' : undefined}
            >
              <th
                scope="row"
                className="sticky left-0 z-10 border bg-background px-2 py-1.5 text-left font-normal"
                title={description.get(r.name)}
              >
                <span className="block font-mono font-medium" title={r.sources?.length ? `元の源泉: ${r.sources.join(', ')}` : undefined}>
                  {r.name}
                  {r.candidate && <span className="ml-1 font-sans text-[10px] text-primary">（候補）</span>}
                </span>
                <span className="block text-muted-foreground">{r.label}</span>
                {!compact && r.sources && r.sources.length > 0 && (
                  <span className="block font-mono text-[10px] text-muted-foreground">← {r.sources.join(', ')}</span>
                )}
              </th>
              {bus.groups.flatMap((g, i) =>
                columns(g).map((a) => {
                  const joined = r.groups.includes(g.name)
                  const candidates = metrics?.get(r.name) ?? []
                  // この属性で切れる指標（無ければ切り口なしで最初の指標）
                  const sliced = compact ? undefined : candidates.find((m) => m.dimensions.includes(a.name))
                  const metric = sliced ?? candidates[0]
                  const check = <Check role="img" className="mx-auto size-3.5" aria-label={`${r.name} × ${a.label}`} />
                  let content: React.ReactNode = joined ? check : null
                  if (joined && metric && !r.candidate) {
                    // ✓ → その fact の指標を、この属性の切り口でチャートに（compact は属性が無いので切り口なし）
                    const label = sliced
                      ? `${metric.label}を${g.kind === 'dim' ? `${g.label}の` : ''}${a.label}別にチャートで見る`
                      : `${metric.label}をチャートで見る（この切り口では切れません）`
                    content = (
                      <a
                        href={chartUrl(metric.name, null, sliced ? a.name : null)}
                        title={label}
                        aria-label={label}
                        className="block py-1 hover:bg-primary/20 focus-visible:outline-2 focus-visible:outline-primary"
                        {...(compact ? { target: '_blank', rel: 'noreferrer' } : {})}
                      >
                        {check}
                      </a>
                    )
                  } else if (joined) {
                    content = <span className="block py-1">{check}</span>
                  } else if (onRequest && !r.candidate && g.kind === 'dim') {
                    // 空欄 → 「<fact>を<dim の属性>別に見たい」を要求に入れる（作れる候補として見せる）。
                    // 別の fact 自身の属性（退化ディメンション）は、この fact とは関係が無いので出さない
                    const text = `「${r.label}」を${g.label}の${a.label}別に見たい`
                    content = (
                      <button
                        type="button"
                        title={`${text}（要求に入れる）`}
                        aria-label={`要求に入れる: ${text}`}
                        className="absolute inset-0 hover:bg-amber-500/20 focus-visible:outline-2 focus-visible:outline-amber-500"
                        onClick={() => onRequest(text)}
                      />
                    )
                  }
                  return (
                    <td key={`${g.name}:${a.name}`} className={cn('relative w-8 min-w-8 max-w-8 border p-0 text-center', shade(i))}>
                      {content}
                      <span className="sr-only">{joined ? '集計できる' : '-'}</span>
                    </td>
                  )
                }),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
