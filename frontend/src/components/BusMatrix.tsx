import { Check } from 'lucide-react'

import type { BusMatrix, ModelInfo } from '@/api/datasets'
import { cn } from '@/lib/utils'

/**
 * Kimball のバスマトリクス（#62）。行が fact、列が dimension（とその属性）。
 * fact が結合できる dimension の属性すべてに ✓ を付ける。fact 自身の属性（退化ディメンション）は最後の列。
 */
export function BusMatrixView({ bus, models }: { bus: BusMatrix; models: ModelInfo[] }) {
  if (bus.rows.length === 0) return <p className="text-xs text-muted-foreground">まだ fact がありません。</p>
  const description = new Map(models.map((m) => [m.name, m.description]))
  // 列のまとまりごとに背景を交互に変える
  const shade = (i: number) => (i % 2 === 0 ? 'bg-muted/60' : '')
  return (
    <div className="overflow-x-auto">
      <table className="border-collapse text-xs" aria-label="バスマトリクス">
        <thead>
          <tr>
            <th rowSpan={2} className="min-w-40 align-bottom" />
            {bus.groups.map((g, i) => (
              <th
                key={g.name}
                colSpan={g.attributes.length}
                scope="colgroup"
                title={`${g.name}${description.get(g.name) ? `: ${description.get(g.name)}` : ''}`}
                className={cn('border px-1 py-1 text-center font-semibold break-all', shade(i))}
              >
                {g.kind === 'dim' ? (
                  <>
                    {g.label}
                    <span className="block font-mono text-[10px] font-normal text-muted-foreground">{g.name}</span>
                  </>
                ) : (
                  <>
                    fact 内の属性
                    <span className="block font-mono text-[10px] font-normal text-muted-foreground">{g.name}</span>
                  </>
                )}
              </th>
            ))}
          </tr>
          <tr>
            {bus.groups.flatMap((g, i) =>
              g.attributes.map((a) => (
                <th
                  key={a.name}
                  scope="col"
                  title={a.name}
                  className={cn('h-28 w-8 min-w-8 max-w-8 border px-0 py-1 align-bottom font-normal', shade(i))}
                >
                  <span className="mx-auto block max-h-26 overflow-hidden [writing-mode:vertical-rl]">{a.label}</span>
                </th>
              )),
            )}
          </tr>
        </thead>
        <tbody>
          {bus.rows.map((r) => (
            <tr key={r.name} data-testid={`bus-${r.name}`}>
              <th scope="row" className="border px-2 py-1.5 text-left font-normal" title={description.get(r.name)}>
                <span className="block font-mono font-medium">{r.name}</span>
                <span className="block text-muted-foreground">{r.label}</span>
              </th>
              {bus.groups.flatMap((g, i) =>
                g.attributes.map((a) => (
                  <td key={a.name} className={cn('w-8 min-w-8 max-w-8 border text-center', shade(i))}>
                    {r.groups.includes(g.name) && (
                      <Check className="mx-auto size-3.5" aria-label={`${r.name} × ${a.label}`} />
                    )}
                  </td>
                )),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
