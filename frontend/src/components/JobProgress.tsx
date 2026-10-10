import { CheckCircle2, Circle, ExternalLink, Loader2, XCircle } from 'lucide-react'

import type { JobView } from '@/api/types'

const ICON = {
  pending: <Circle className="size-4 text-muted-foreground" />,
  running: <Loader2 className="size-4 animate-spin" />,
  done: <CheckCircle2 className="size-4 text-green-600" />,
  failed: <XCircle className="size-4 text-destructive" />,
}

export function JobProgress({ job }: { job: JobView }) {
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
      {job.error && <p className="text-sm text-destructive">{job.error}</p>}
    </div>
  )
}
