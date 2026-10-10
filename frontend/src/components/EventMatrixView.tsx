import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { EventMatrix } from '@/generated/beam'

const LEVEL: Record<string, string> = { high: '高', medium: '中', low: '低' }

export function EventMatrixView({ matrix }: { matrix: EventMatrix }) {
  if (matrix.rows.length === 0) {
    return <p className="text-sm text-muted-foreground">まだイベントがありません。</p>
  }
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>イベント</TableHead>
            <TableHead>種類</TableHead>
            <TableHead>優先度</TableHead>
            <TableHead>インパクト</TableHead>
            <TableHead>工数</TableHead>
            {matrix.columns.map((c) => (
              <TableHead key={c.name} className="text-center">
                {c.label}
                {c.conformed && (
                  <Badge variant="outline" className="ml-1">
                    共通
                  </Badge>
                )}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {matrix.rows.map((r) => (
            <TableRow key={r.event}>
              <TableCell className="font-medium">{r.label}</TableCell>
              <TableCell>{r.fact_type}</TableCell>
              <TableCell>{r.priority ? LEVEL[r.priority] : '-'}</TableCell>
              <TableCell>{r.impact ? LEVEL[r.impact] : '-'}</TableCell>
              <TableCell>{r.effort ? LEVEL[r.effort] : '-'}</TableCell>
              {matrix.columns.map((c) => (
                <TableCell key={c.name} className="text-center" aria-label={`${r.label} × ${c.label}`}>
                  {r.dimensions[c.name] ? '✓' : ''}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
