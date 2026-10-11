import { useState } from 'react'

import { finalizeBlockers } from '@/components/finalizeBlockers'
import { Button } from '@/components/ui/button'

const MAX_ISSUES = 8

export function FinalizeControl({
  issues,
  eventCount,
  busy,
  demo,
  finalized,
  onFinalize,
}: {
  issues: string[]
  eventCount: number
  busy: boolean
  demo?: boolean
  finalized?: boolean
  onFinalize: () => Promise<void>
}) {
  const [submitting, setSubmitting] = useState(false)
  const blockers = finalizeBlockers({ issues, eventCount, busy, submitting, demo, finalized })
  const shown = blockers.issues.slice(0, MAX_ISSUES)
  const rest = blockers.issues.length - shown.length

  const click = async () => {
    setSubmitting(true)
    try {
      await onFinalize()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-2">
      {shown.length > 0 && (
        <div>
          <p className="text-xs font-semibold text-muted-foreground">確定までに残っている課題</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground" aria-label="確定までに残っている課題">
            {shown.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
          {rest > 0 && <p className="mt-0.5 pl-5 text-xs text-muted-foreground">ほか {rest} 件</p>}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          onClick={click}
          disabled={blockers.reason !== null}
          aria-describedby={blockers.reason ? 'finalize-reason' : undefined}
        >
          確定して実装へ
        </Button>
        {blockers.reason && (
          <p id="finalize-reason" role="status" className="text-xs text-muted-foreground">
            {blockers.reason}
          </p>
        )}
      </div>
    </div>
  )
}
