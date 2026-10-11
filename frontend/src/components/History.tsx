import { ArrowLeft, ExternalLink, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api, errorText } from '@/api/client'
import { PHASES, type SessionSummary } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

const PR_STATE: Record<string, { label: string; className: string }> = {
  OPEN: { label: 'レビュー待ち', className: 'border-sky-500 text-sky-600' },
  MERGED: { label: 'マージ済み', className: 'border-green-600 text-green-700' },
  CLOSED: { label: 'クローズ', className: 'text-muted-foreground' },
}
/** GitHub の PR の URL だけをリンクにする（それ以外の値をそのまま href にしない） */
const GITHUB_PR = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+$/

const JOB_STATE: Record<string, string> = { running: '生成中', succeeded: 'PR 作成済み', failed: '失敗' }

function status(s: SessionSummary): string {
  if (s.job) return JOB_STATE[s.job.status] ?? s.job.status
  return PHASES.find((p) => p.id === s.phase)?.label ?? s.phase
}

/** これまでの要求と、生成した PR の一覧（#73） */
export function HistoryScreen() {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api
      .history()
      .then((d) => setSessions(d.sessions))
      .catch((e) => setError(errorText(e)))
  }, [])

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="size-4" /> ホーム
      </a>
      <h1 className="text-xl font-bold">これまでの要求と PR</h1>
      {error && <p className="text-sm text-destructive">一覧を読み込めませんでした: {error}</p>}
      {!sessions && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> 読み込んでいます…
        </p>
      )}
      {sessions && sessions.length === 0 && <p className="text-sm text-muted-foreground">まだ要求がありません。</p>}
      {sessions && sessions.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>要求</TableHead>
              <TableHead>状態</TableHead>
              <TableHead>PR</TableHead>
              <TableHead>作った指標</TableHead>
              <TableHead>更新</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sessions.map((s) => {
              const pr = s.pr_state ? PR_STATE[s.pr_state] : null
              return (
                <TableRow key={s.id} data-testid={`session-${s.id}`} className="align-top">
                  <TableCell className="whitespace-normal">
                    <a href={`?session=${encodeURIComponent(s.id)}`} className="font-medium text-primary underline">
                      {s.title || s.request || s.id}
                    </a>
                    {s.title && s.request && <p className="text-xs text-muted-foreground">{s.request}</p>}
                  </TableCell>
                  <TableCell className="text-xs">{status(s)}</TableCell>
                  <TableCell className="text-xs">
                    {s.job?.pr_url && GITHUB_PR.test(s.job.pr_url) ? (
                      <span className="flex flex-col items-start gap-1">
                        <a
                          href={s.job.pr_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-primary underline"
                        >
                          {s.job.pr_url.replace(/^https:\/\/github\.com\//, '')} <ExternalLink className="size-3" />
                        </a>
                        {pr && (
                          <Badge variant="outline" className={pr.className}>
                            {pr.label}
                          </Badge>
                        )}
                      </span>
                    ) : (
                      '-'
                    )}
                  </TableCell>
                  <TableCell className="text-xs whitespace-normal">{s.metrics.join('、') || '-'}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {new Date(s.updated_at).toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' })}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </main>
  )
}
