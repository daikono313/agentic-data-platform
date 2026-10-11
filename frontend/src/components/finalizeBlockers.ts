export interface FinalizeBlockers {
  /** ボタンを押せない理由（押せるときは null） */
  reason: string | null
  /** 確定までに残っている課題（backend の Interviewer.issues） */
  issues: string[]
}

/**
 * 「確定して実装へ」を押せない理由（#108）。ボタンの無効の条件と、画面に出す理由を同じ場所で決める。
 */
export function finalizeBlockers(input: {
  issues: string[]
  eventCount: number
  busy: boolean
  submitting?: boolean
  demo?: boolean
  finalized?: boolean
}): FinalizeBlockers {
  const { issues, eventCount, busy, submitting, demo, finalized } = input
  if (finalized) return { reason: '確定済みです。', issues: [] }
  if (demo) return { reason: 'デモでは確定できません。', issues }
  if (submitting) return { reason: '確定しています…', issues: [] }
  if (busy) return { reason: '応答の途中です。応答が終わると確定できます。', issues }
  if (eventCount === 0)
    return { reason: 'まだ出来事（誰が・何をする）が記録されていません。インタビューを続けてください。', issues }
  if (issues.length > 0)
    return {
      reason: `確定までに残っている課題が ${issues.length} 件あります。インタビューで解消すると確定できます。`,
      issues,
    }
  return { reason: null, issues }
}
