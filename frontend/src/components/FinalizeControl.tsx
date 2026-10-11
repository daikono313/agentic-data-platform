import { useId, useState } from 'react'

import { finalizeBlockers } from '@/components/finalizeBlockers'
import { Button } from '@/components/ui/button'

const MAX_ISSUES = 8

/**
 * 「確定して実装へ」のボタンと、押せない理由・確定に失敗した理由（#108）。
 * onFinalize は失敗しても投げず、error で失敗の理由を渡す。
 */
export function FinalizeControl({
  issues,
  eventCount,
  busy,
  demo,
  error,
  onFinalize,
}: {
  issues: string[]
  eventCount: number
  busy: boolean
  demo?: boolean
  /** 直前の確定の失敗（backend の 409 などの detail） */
  error?: string | null
  onFinalize: () => Promise<void>
}) {
  const [submitting, setSubmitting] = useState(false)
  const reasonId = useId()
  const errorId = useId()
  const blockers = finalizeBlockers({ issues, eventCount, busy, submitting, demo })
  const shown = blockers.issues.slice(0, MAX_ISSUES)
  const rest = blockers.issues.length - shown.length
  const describedBy = [blockers.reason && reasonId, error && errorId].filter(Boolean).join(' ') || undefined

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
        <Button onClick={click} disabled={blockers.reason !== null} aria-describedby={describedBy}>
          確定して実装へ
        </Button>
        {blockers.reason && (
          <p id={reasonId} className="text-xs text-muted-foreground">
            {blockers.reason}
          </p>
        )}
        {/* 読み上げは、押したあとの「確定しています」だけ（理由が変わるたびには読まない） */}
        <span aria-live="polite" className="sr-only">
          {submitting ? blockers.reason : ''}
        </span>
      </div>
      {error && !submitting && (
        <p id={errorId} role="alert" className="text-xs text-destructive">
          確定できませんでした: {error}
        </p>
      )}
    </div>
  )
}
