import { PHASES, type Phase } from '@/api/types'
import { cn } from '@/lib/utils'

export function PhaseProgress({ phase }: { phase: Phase }) {
  const current = PHASES.findIndex((p) => p.id === phase)
  return (
    <ol className="flex flex-wrap gap-1 text-xs" aria-label="インタビューの進み具合">
      {PHASES.map((p, i) => (
        <li
          key={p.id}
          aria-current={i === current ? 'step' : undefined}
          className={cn(
            'rounded-full border px-2 py-0.5',
            i < current && 'border-primary/30 text-muted-foreground',
            i === current && 'border-primary bg-primary text-primary-foreground',
            i > current && 'text-muted-foreground opacity-60',
          )}
        >
          {p.label}
        </li>
      ))}
    </ol>
  )
}
