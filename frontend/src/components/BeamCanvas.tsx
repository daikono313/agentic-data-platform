import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { BusinessEvent, Dimension, W } from '@/generated/beam'

const W_LABEL: Record<W, string> = {
  who: 'Who（誰が）',
  what: 'What（何を）',
  when: 'When（いつ）',
  where: 'Where（どこで）',
  why: 'Why（なぜ）',
  how: 'How（どのように）',
}

function WCell({ w, dims }: { w: W; dims: Dimension[] }) {
  return (
    <Card className="gap-2 py-3" data-testid={`w-${w}`}>
      <CardHeader className="px-3">
        <CardTitle className="text-xs font-semibold text-muted-foreground">{W_LABEL[w]}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 px-3">
        {dims.length === 0 && <p className="text-xs text-muted-foreground">未確認</p>}
        {dims.map((d) => (
          <div key={d.role ?? d.name}>
            <p className="text-sm font-medium">
              {d.label}
              {d.role && <span className="ml-1 text-xs text-muted-foreground">（{d.role}）</span>}
              {d.time_grain && <span className="ml-1 text-xs text-muted-foreground">最小: {d.time_grain}</span>}
            </p>
            <div className="mt-1 flex flex-wrap gap-1">
              {(d.attributes ?? []).map((a) => (
                <Badge
                  key={a.name}
                  variant={a.availability === 'unavailable' ? 'destructive' : 'secondary'}
                  title={a.examples?.length ? `例: ${a.examples.join(', ')}` : undefined}
                  className={a.availability === 'unavailable' ? 'line-through' : undefined}
                >
                  {a.label}
                  {a.availability === 'unavailable' && '（データなし）'}
                </Badge>
              ))}
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export function BeamCanvas({ event }: { event?: BusinessEvent }) {
  if (!event) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        出来事（ビジネスイベント）が決まると、ここに BEAM Canvas が表示されます。
      </div>
    )
  }
  const by = (w: W) => (event.dimensions ?? []).filter((d) => d.w === w)
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="font-semibold">{event.label}</h3>
        <span className="text-xs text-muted-foreground">
          {event.subject} が {event.object} を {event.verb}
        </span>
        {event.grain && <Badge variant="outline">1 行 = {event.grain}</Badge>}
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        <WCell w="who" dims={by('who')} />
        <WCell w="when" dims={by('when')} />
        <WCell w="where" dims={by('where')} />
        <WCell w="what" dims={by('what')} />
        <Card className="gap-2 border-primary/40 bg-primary/5 py-3" data-testid="how-many">
          <CardHeader className="px-3">
            <CardTitle className="text-xs font-semibold">How many（いくつ・いくら）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 px-3">
            {(event.measures ?? []).length === 0 && (
              <p className="text-xs text-muted-foreground">未確認</p>
            )}
            {(event.measures ?? []).map((m) => (
              <p
                key={m.name}
                className={m.availability === 'unavailable' ? 'text-sm text-destructive line-through' : 'text-sm'}
              >
                {m.label}
                <span className="ml-1 text-xs text-muted-foreground">
                  {m.aggregation}
                  {m.unit ? ` / ${m.unit}` : ''}
                </span>
              </p>
            ))}
          </CardContent>
        </Card>
        <WCell w="why" dims={by('why')} />
        <WCell w="how" dims={by('how')} />
      </div>
    </div>
  )
}
