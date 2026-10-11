import { ArrowLeft, Bot, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { api, errorText, trackJob } from '@/api/client'
import { sourceUrl, type OnboardingDraft, type OntologyPatch } from '@/api/datasets'
import type { JobView } from '@/api/types'
import { BusMatrixView } from '@/components/BusMatrix'
import { JobProgress } from '@/components/JobProgress'
import { MermaidDiagram } from '@/components/Ontology'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

const SELECT = 'h-8 rounded-md border bg-background px-2 text-xs'

/**
 * 源泉の取り込み（#69）。オントロジーへの追加案 → クレンジングの定義 → バスマトリクスの差分を確認して、
 * PR にする。fact / dim と指標は、その後のインタビューで作る。
 */
export function OnboardingScreen({ table }: { table: string }) {
  const [draft, setDraft] = useState<OnboardingDraft | null>(null)
  const [patch, setPatch] = useState<OntologyPatch | null>(null)
  const [cleansing, setCleansing] = useState('')
  /** 利用者が直したクレンジングの定義は、案を作り直しても上書きしない */
  const [cleansingEdited, setCleansingEdited] = useState(false)
  const [redo, setRedo] = useState(false)
  const [loading, setLoading] = useState<'profile' | 'agent' | null>('profile')
  const [error, setError] = useState<string | null>(null)
  const [job, setJob] = useState<JobView | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const load = useCallback(
    async (agent: boolean) => {
      setLoading(agent ? 'agent' : 'profile')
      setError(null)
      try {
        const d = await api.onboardingDraft(table, agent)
        setDraft(d)
        setPatch(d.patch)
        setCleansing((current) => (cleansingEdited ? current : d.cleansing))
      } catch (e) {
        setError(errorText(e))
      } finally {
        setLoading(null)
      }
    },
    [table, cleansingEdited],
  )

  const follow = useCallback((initial: JobView) => trackJob(initial, setJob), [])

  useEffect(() => {
    // 実行中・実行済みの取り込みがあればそれを出す。無ければ下書きを作る
    api
      .onboardingStatus(table)
      .then(({ job: j }) => {
        if (j && j.status !== 'failed') {
          setLoading(null)
          if (j.status === 'running') follow(j).catch((e) => setError(errorText(e)))
          else setJob(j)
        } else {
          load(false)
        }
      })
      .catch(() => load(false))
  }, [table, load, follow])

  const submit = async () => {
    if (!patch) return
    setSubmitting(true)
    setError(null)
    try {
      await follow(await api.startOnboarding(table, patch, cleansing, redo))
    } catch (e) {
      setError(errorText(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="size-4" /> ホーム
      </a>
      <header className="space-y-1">
        <h1 className="text-xl font-bold">源泉を取り込む</h1>
        <p className="text-sm text-muted-foreground">
          <a href={sourceUrl(table)} className="font-mono text-primary underline">
            {table}
          </a>{' '}
          を、オントロジー（業務の概念）に足し、クレンジング層を作ります。確定すると PR ができます。fact / dim
          と指標は、その後に「◯◯を見たい」と要求すると作れます。
        </p>
      </header>
      {error && <p className="rounded border border-destructive p-2 text-sm text-destructive">{error}</p>}
      {job ? (
        <Card className="gap-2 py-3">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">取り込み</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <JobProgress job={job} />
            {job.status === 'failed' && (
              <Button variant="outline" size="sm" className="mt-2" onClick={() => setJob(null)}>
                修正してやり直す
              </Button>
            )}
            {job.status === 'succeeded' && (
              <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                <p>PR をマージすると、この源泉がオントロジーに入ります。</p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    // 前の PR を閉じた（マージしなかった）場合だけ、作り直す
                    setRedo(true)
                    setJob(null)
                    if (!draft) load(false)
                  }}
                >
                  PR を閉じたので、やり直す
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
        <>
          {loading && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {loading === 'agent' ? 'エージェントが源泉を調べて案を作っています（1 分ほど）…' : '下書きを作っています…'}
            </p>
          )}
          {draft && patch && (
            <DraftBody
              draft={draft}
              patch={patch}
              onPatch={setPatch}
              cleansing={cleansing}
              onCleansing={(s) => {
                setCleansing(s)
                setCleansingEdited(true)
              }}
              onAgent={() => load(true)}
              busy={loading !== null}
            />
          )}
          {draft && patch && (
            <div className="flex items-center gap-3">
              <Button onClick={submit} disabled={submitting || loading !== null}>
                {submitting ? '送信しています…' : '確定して PR を作る'}
              </Button>
              <span className="text-xs text-muted-foreground">確定の前に、追加案とクレンジングの定義を検証します</span>
            </div>
          )}
        </>
      )}
    </main>
  )
}

function DraftBody({
  draft,
  patch,
  onPatch,
  cleansing,
  onCleansing,
  onAgent,
  busy,
}: {
  draft: OnboardingDraft
  patch: OntologyPatch
  onPatch: (p: OntologyPatch) => void
  cleansing: string
  onCleansing: (s: string) => void
  onAgent: () => void
  busy: boolean
}) {
  const update = (i: number, change: Partial<OntologyPatch['concepts'][number]>) =>
    onPatch({ ...patch, concepts: patch.concepts.map((c, j) => (j === i ? { ...c, ...change } : c)) })
  return (
    <>
      <Card className="gap-2 py-3" aria-label="オントロジーへの追加案">
        <CardHeader className="px-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-sm">1. オントロジーへの追加案</CardTitle>
            <div className="flex items-center gap-2">
              <Badge variant="outline">{draft.proposer === 'agent' ? 'エージェントの案' : 'プロファイルからの案'}</Badge>
              <Button variant="outline" size="sm" onClick={onAgent} disabled={busy}>
                <Bot className="size-4" /> エージェントに案を作らせる
              </Button>
            </div>
          </div>
          {draft.note && <p className="text-xs text-muted-foreground">{draft.note}</p>}
        </CardHeader>
        <CardContent className="space-y-3 px-4">
          {patch.concepts.map((c, i) => (
            <div key={i} className="space-y-2 rounded-md border p-3" data-testid={`concept-${c.name}`}>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-mono">{c.name}</span>
                <Input
                  aria-label="概念の名前（表示名）"
                  className="h-8 w-48 text-sm"
                  value={c.label}
                  onChange={(e) => update(i, { label: e.target.value })}
                />
                <select
                  aria-label="種類"
                  className={SELECT}
                  value={c.kind}
                  onChange={(e) => update(i, { kind: e.target.value as 'entity' | 'event' })}
                >
                  <option value="event">出来事（fact の候補）</option>
                  <option value="entity">もの・人・場所（dim の候補）</option>
                </select>
                <span className="text-muted-foreground">
                  キー <span className="font-mono">{c.key}</span>
                </span>
              </div>
              <ul className="grid gap-1 sm:grid-cols-2">
                {c.attributes.map((a, k) => (
                  <li key={a.name} className="flex items-center gap-2 text-xs">
                    <span className="w-32 shrink-0 font-mono text-muted-foreground">{a.column}</span>
                    <Input
                      aria-label={`属性 ${a.column} の表示名`}
                      className="h-7 text-xs"
                      value={a.label}
                      onChange={(e) =>
                        update(i, {
                          attributes: c.attributes.map((x, j) => (j === k ? { ...x, label: e.target.value } : x)),
                        })
                      }
                    />
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {patch.relations.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-semibold">既存の概念との関係</p>
              <ul className="space-y-1">
                {patch.relations.map((r, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2 text-xs">
                    <span>{r.from}</span>→
                    <Input
                      aria-label={`関係 ${r.from} → ${r.to} の動詞`}
                      className="h-7 w-32 text-xs"
                      value={r.verb}
                      onChange={(e) =>
                        onPatch({
                          ...patch,
                          relations: patch.relations.map((x, j) => (j === i ? { ...x, verb: e.target.value } : x)),
                        })
                      }
                    />
                    →<span>{r.to}</span>
                    <span className="font-mono text-muted-foreground">{r.via}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {patch.concepts.some((c, i) => c.kind !== draft.patch.concepts[i]?.kind) && (
            <p className="text-xs text-amber-600">
              種類を変えました。下の図と fact / dim の候補は元の案のままです（確定すると、直した内容で作ります）。
            </p>
          )}
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">追加後のオントロジーの図（元の案）</summary>
            <MermaidDiagram text={draft.mermaid} />
          </details>
        </CardContent>
      </Card>

      <Card className="gap-2 py-3" aria-label="クレンジングの定義">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">2. クレンジングの定義</CardTitle>
          <p className="text-xs text-muted-foreground">
            {draft.cleansing_exists ? '今の定義です。' : 'プロファイルから作った雛形です。'}
            確定すると <code className="font-mono">warehouse/cleansing/{draft.source}.yml</code>{' '}
            に置き、クレンジング層（stg）を生成します。
          </p>
        </CardHeader>
        <CardContent className="px-4">
          <textarea
            aria-label="クレンジングの定義（YAML）"
            className="h-56 w-full rounded-md border bg-muted p-3 font-mono text-xs"
            value={cleansing}
            onChange={(e) => onCleansing(e.target.value)}
            spellCheck={false}
          />
        </CardContent>
      </Card>

      <Card className="gap-2 py-3" aria-label="バスマトリクスの差分">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">3. 取り込んだ後に作れる fact / dim（候補）</CardTitle>
          <p className="text-xs text-muted-foreground">
            点線の行・列が候補です（fct の名前は、インタビューで決まる出来事の名前になります）。インタビューで指標を要求すると、この組み合わせで
            fact / dim ができます。
          </p>
        </CardHeader>
        <CardContent className="px-4">
          <BusMatrixView bus={draft.bus} models={[]} />
        </CardContent>
      </Card>
    </>
  )
}
