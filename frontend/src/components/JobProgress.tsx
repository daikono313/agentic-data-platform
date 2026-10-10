import { BarChart3, CheckCircle2, Circle, ExternalLink, Loader2, XCircle } from 'lucide-react'

import { chartUrl } from '@/api/charts'
import type { JobView } from '@/api/types'

const ICON = {
  pending: <Circle className="size-4 text-muted-foreground" />,
  running: <Loader2 className="size-4 animate-spin" />,
  done: <CheckCircle2 className="size-4 text-green-600" />,
  failed: <XCircle className="size-4 text-destructive" />,
}

export function JobProgress({ job, metrics = [] }: { job: JobView; metrics?: { name: string; label: string }[] }) {
  return (
    <div className="space-y-2" aria-live="polite">
      <ul className="space-y-1">
        {job.steps.map((s) => (
          <li key={s.step} className="flex items-start gap-2 text-sm">
            {ICON[s.status]}
            <div>
              <p>{s.label}</p>
              {s.detail && <p className="text-xs text-muted-foreground">{s.detail}</p>}
            </div>
          </li>
        ))}
      </ul>
      {job.pr_url && (
        <a
          href={job.pr_url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-sm font-medium text-primary underline"
        >
          作成した PR を開く <ExternalLink className="size-3" />
        </a>
      )}
      {job.pr_url && job.branch && metrics.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">作った指標をチャートで見る（PR の内容でプレビュー）</p>
          <ul className="flex flex-wrap gap-2">
            {metrics.map((m) => (
              <li key={m.name}>
                <a
                  href={chartUrl(m.name, job.id)}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-sm hover:bg-muted"
                >
                  <BarChart3 className="size-3" /> {m.label}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {job.error && <p className="text-sm text-destructive">{job.error}</p>}
    </div>
  )
}
