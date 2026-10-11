import { ArrowLeft, KeyRound, Link2, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { errorText, type CleansingView, type ColumnProfile, type SourceProfile } from '@/api/datasets'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

function pct(n: number, total: number): string {
  if (!total) return '-'
  const v = (n / total) * 100
  return n > 0 && v < 0.1 ? '<0.1%' : `${v.toFixed(1)}%`
}

/** 源泉 1 つのデータプロファイル（#62）。列ごとの統計と、キーの候補・使っているモデル */
export function SourceProfileScreen({ table }: { table: string }) {
  const [profile, setProfile] = useState<SourceProfile | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .sourceProfile(table)
      .then(setProfile)
      .catch((e) => setError(errorText(e)))
  }, [table])

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="size-4" /> ホーム
      </a>
      <header>
        <p className="font-mono text-xs text-muted-foreground">{table}</p>
        <h1 className="text-xl font-bold">{profile?.description.split('\n')[0] || table}</h1>
      </header>
      {error && <p className="text-sm text-destructive">プロファイルを取得できませんでした: {error}</p>}
      {!profile && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> プロファイルを計算しています…
        </p>
      )}
      {profile && <ProfileBody profile={profile} />}
      {profile && <CleansingCard key={table} table={table} />}
    </main>
  )
}

function ProfileBody({ profile }: { profile: SourceProfile }) {
  const descriptions = new Map(profile.columns.map((c) => [c.name, c.description]))
  const fks = new Map(profile.foreign_key_candidates.map((f) => [f.column, f.references]))
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="行数" value={profile.row_count.toLocaleString()} />
        <Stat label="列数" value={String(profile.profiles.length)} />
        <Card className="gap-1 py-3">
          <CardContent className="space-y-1 px-4 text-xs">
            <p className="text-muted-foreground">使っている整備済みモデル</p>
            {profile.used_by.length ? (
              <div className="flex flex-wrap gap-1">
                {profile.used_by.map((m) => (
                  <Badge key={m} variant="secondary" className="font-mono">
                    {m}
                  </Badge>
                ))}
              </div>
            ) : (
              <p>まだありません</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="gap-2 py-3">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">列のプロファイル</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>列</TableHead>
                <TableHead>型</TableHead>
                <TableHead className="text-right">NULL</TableHead>
                <TableHead className="text-right">ユニーク数</TableHead>
                <TableHead>最小 〜 最大</TableHead>
                <TableHead>よく出る値</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {profile.profiles.map((p) => (
                <ColumnRow
                  key={p.column}
                  profile={p}
                  description={descriptions.get(p.column) ?? ''}
                  isKey={profile.primary_key_candidates.includes(p.column)}
                  references={fks.get(p.column)}
                />
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">
        <KeyRound className="inline size-3" /> 主キー候補（NULL なし・値が重複しない）、
        <Link2 className="inline size-3" /> 外部キー候補（値の 95% 以上が参照先に存在する）。いずれも値から推定したものです。
      </p>
    </>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Card className="gap-1 py-3">
      <CardContent className="px-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="text-lg font-semibold">{value}</p>
      </CardContent>
    </Card>
  )
}

function ColumnRow({
  profile: p,
  description,
  isKey,
  references,
}: {
  profile: ColumnProfile
  description: string
  isKey: boolean
  references?: string
}) {
  // NULL 以外の値がすべて違う列（ID・メールアドレスなど）は、上位の値を出しても意味がない
  const nonNull = p.row_count - p.null_count
  const allDistinct = p.distinct_count > 0 && p.distinct_count === nonNull
  // 値の種類が多く、最頻値でも 1% 未満の列（日時など）は、ほぼすべて違う値として扱う
  const topShare = nonNull > 0 ? (p.top_values.find(([v]) => v !== null)?.[1] ?? 0) / nonNull : 0
  const mostlyDistinct = p.distinct_count > 100 && topShare < 0.01
  const showTop = p.distinct_count > 0 && !allDistinct && !mostlyDistinct
  return (
    <TableRow data-testid={`column-${p.column}`} className="align-top">
      <TableCell className="whitespace-normal">
        <p className="flex items-center gap-1 font-mono text-xs">
          {p.column}
          {isKey && <KeyRound className="size-3 text-primary" aria-label="主キー候補" />}
          {references && <Link2 className="size-3 text-primary" aria-label={`外部キー候補: ${references}`} />}
        </p>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
        {references && <p className="text-xs text-muted-foreground">→ {references}</p>}
      </TableCell>
      <TableCell className="font-mono text-xs">{p.data_type}</TableCell>
      <TableCell className="text-right text-xs">{pct(p.null_count, p.row_count)}</TableCell>
      <TableCell className="text-right text-xs">{p.distinct_count.toLocaleString()}</TableCell>
      <TableCell className="max-w-48 truncate text-xs" title={`${p.min ?? ''} 〜 ${p.max ?? ''}`}>
        {p.min === null ? '-' : `${p.min} 〜 ${p.max}`}
      </TableCell>
      <TableCell className="whitespace-normal text-xs">
        {showTop ? (
          <div className="flex flex-wrap gap-1">
            {p.top_values.map(([v, n], i) => (
              <Badge key={i} variant="outline" title={`${n.toLocaleString()} 行`}>
                {v ?? 'NULL'}
                <span className="ml-1 text-muted-foreground">{pct(n, p.row_count)}</span>
              </Badge>
            ))}
          </div>
        ) : (
          <span className="text-muted-foreground">{p.distinct_count === 0 ? 'すべて NULL' : allDistinct ? 'すべて異なる値' : 'ほとんど異なる値'}</span>
        )}
      </TableCell>
    </TableRow>
  )
}

/** クレンジングの定義（#68）。今の定義か、プロファイルから作った雛形を出す */
function CleansingCard({ table }: { table: string }) {
  const [view, setView] = useState<CleansingView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showDraft, setShowDraft] = useState(false)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    api
      .sourceCleansing(table)
      .then(setView)
      .catch((e) => setError(errorText(e)))
  }, [table])
  if (error) return <p className="text-xs text-destructive">クレンジングの定義を取得できませんでした: {error}</p>
  if (!view) return null
  const text = (view.existing && !showDraft ? view.existing : view.draft) ?? view.draft_error ?? ''

  const copy = () => {
    navigator.clipboard
      ?.writeText(text)
      .then(() => setCopied(true))
      .catch(() => setCopied(false))
  }
  return (
    <Card className="gap-2 py-3" aria-label="クレンジングの定義">
      <CardHeader className="px-4">
        <CardTitle className="text-sm">クレンジングの定義</CardTitle>
        <p className="text-xs text-muted-foreground">
          {view.existing ? (
            <>
              <code className="font-mono">{view.path}</code> から、クレンジング層（stg）を生成しています。
            </>
          ) : (
            <>
              まだ定義がありません。下の雛形（プロファイルから作成）を確認して{' '}
              <code className="font-mono">{view.path}</code> に置き、<code className="font-mono">make cleansing</code>{' '}
              を実行すると、クレンジング層（stg）ができます。
            </>
          )}
        </p>
      </CardHeader>
      <CardContent className="space-y-2 px-4">
        <div className="flex gap-2 text-xs">
          {view.existing && view.draft && (
            <button type="button" className="underline" onClick={() => setShowDraft((d) => !d)}>
              {showDraft ? '今の定義を見る' : 'プロファイルからの雛形を見る'}
            </button>
          )}
          <button type="button" className="underline" onClick={copy}>
            {copied ? 'コピーしました' : 'コピー'}
          </button>
        </div>
        <pre className="max-h-96 overflow-auto rounded bg-muted p-3 font-mono text-xs" data-testid="cleansing-yaml">
          {text}
        </pre>
      </CardContent>
    </Card>
  )
}
