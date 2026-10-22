import { ArrowLeft, Bot, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import { api, errorText, trackJob } from '@/api/client'
import {
  sourceUrl,
  type CleansingRule,
  type CleansingSpec,
  type ColumnProfile,
  type OnboardingDraft,
  type OntologyConcept,
  type OntologyPatch,
} from '@/api/datasets'
import type { JobView } from '@/api/types'
import { BusMatrixView } from '@/components/BusMatrix'
import { JobProgress } from '@/components/JobProgress'
import { MermaidDiagram } from '@/components/Ontology'
import { TopValues } from '@/components/SourceProfile'
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
  /** フォームで直す定義。既存の定義が読めなければ null で、YAML（cleansing）を直す */
  const [spec, setSpec] = useState<CleansingSpec | null>(null)
  /**
   * 利用者が直したクレンジングの定義は、案を作り直しても上書きしない。ref にして load の依存から外す
   * （state だと、直した時点で load が作り直され、下書きを読み直して直した表示名を消してしまう）
   */
  const cleansingEdited = useRef(false)
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
        setPatch(blankUntranslated(d.patch))
        if (!cleansingEdited.current) {
          setCleansing(d.cleansing)
          setSpec(d.cleansing_spec ?? null)
        }
      } catch (e) {
        setError(errorText(e))
      } finally {
        setLoading(null)
      }
    },
    [table],
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
      await follow(await api.startOnboarding(table, withFallbackLabels(patch), spec ?? cleansing, redo))
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
                cleansingEdited.current = true
              }}
              spec={spec}
              onSpec={(s) => {
                setSpec(s)
                cleansingEdited.current = true
              }}
              onYaml={() => {
                // フォームで直した内容は捨て、案の YAML から直す
                setSpec(null)
                setCleansing(draft.cleansing)
                cleansingEdited.current = true
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
  spec,
  onSpec,
  onYaml,
  onAgent,
  busy,
}: {
  draft: OnboardingDraft
  patch: OntologyPatch
  onPatch: (p: OntologyPatch) => void
  cleansing: string
  onCleansing: (s: string) => void
  spec: CleansingSpec | null
  onSpec: (s: CleansingSpec) => void
  onYaml: () => void
  onAgent: () => void
  busy: boolean
}) {
  const update = (i: number, change: Partial<OntologyPatch['concepts'][number]>) =>
    onPatch({ ...patch, concepts: patch.concepts.map((c, j) => (j === i ? { ...c, ...change } : c)) })
  const profiles = new Map((draft.columns ?? []).map((p) => [p.column, p]))
  /** 源泉の列 → 属性の表示名（クレンジングのフォームで、列を日本語で示す） */
  const columnLabels = new Map(
    patch.concepts.flatMap((c) =>
      c.attributes.filter((a) => a.label.trim()).map((a) => [a.column, a.label] as const),
    ),
  )
  const untranslated = patch.concepts.reduce(
    (n, c) => n + [c.label, ...c.attributes.map((a) => a.label)].filter((l) => !l.trim()).length,
    0,
  )
  const conceptLabel = (name: string): ReactNode => {
    const own = patch.concepts.find((c) => c.name === name)
    const text = own ? own.label.trim() : (draft.concept_labels?.[name] ?? '')
    return text && !(own === undefined && isUntranslated(text)) ? (
      <span>{text}</span>
    ) : (
      <span>
        <span className="text-muted-foreground">（表示名なし）</span> <Code>{name}</Code>
      </span>
    )
  }
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
            <div key={i} className="space-y-3 rounded-md border p-3" data-testid={`concept-${c.name}`}>
              <dl className="grid items-center gap-x-3 gap-y-2 text-xs sm:grid-cols-[9rem_1fr]">
                <dt className="font-semibold">概念</dt>
                <dd className="flex flex-wrap items-center gap-2">
                  <Input
                    aria-label="概念の名前（表示名）"
                    className="h-8 w-56 text-sm"
                    placeholder={UNTRANSLATED}
                    value={c.label}
                    onChange={(e) => update(i, { label: e.target.value })}
                  />
                  <Code title="内部の名前">{c.name}</Code>
                </dd>
                <dt className="font-semibold">種類</dt>
                <dd>
                  <select
                    aria-label="種類"
                    className={SELECT}
                    value={c.kind}
                    onChange={(e) => update(i, { kind: e.target.value as OntologyConcept['kind'] })}
                  >
                    <option value="event">出来事（fact の候補）</option>
                    <option value="state">状態（定期的に測る値。fact の候補）</option>
                    <option value="entity">もの・人・場所（dim の候補）</option>
                  </select>
                </dd>
                <dt className="font-semibold">1 件を見分けるキー</dt>
                <dd data-testid={`concept-key-${c.name}`}>
                  {c.attributes.find((a) => a.column === c.key)?.label.trim() || columnLabels.get(c.key) || (
                    <span className="text-muted-foreground">（表示名なし）</span>
                  )}{' '}
                  <Code title="列の名前">{c.key}</Code>
                </dd>
              </dl>
              <div className="space-y-1">
                <p className="text-xs font-semibold">属性（表示名を直せます。右は実際の値の例）</p>
                <ul className="divide-y">
                  {c.attributes.map((a, k) => (
                    <li
                      key={a.name}
                      className="grid gap-2 py-1.5 text-xs sm:grid-cols-[14rem_1fr]"
                      data-testid={`attribute-${a.column}`}
                    >
                      <div className="space-y-0.5">
                        <Input
                          aria-label={`属性 ${a.column} の表示名`}
                          className="h-7 text-xs"
                          placeholder={UNTRANSLATED}
                          value={a.label}
                          onChange={(e) =>
                            update(i, {
                              attributes: c.attributes.map((x, j) => (j === k ? { ...x, label: e.target.value } : x)),
                            })
                          }
                        />
                        <Code title="列の名前">{a.column}</Code>
                      </div>
                      <ValueHint profile={profiles.get(a.column)} kind={draft.column_kinds?.[a.column]} />
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          ))}
          {untranslated > 0 && (
            <p className="text-xs text-amber-600" data-testid="untranslated">
              日本語の表示名が未入力の項目が {untranslated} 件あります。未入力のまま確定すると、英語の名前で登録します。
            </p>
          )}
          {patch.relations.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-semibold">既存の概念との関係</p>
              <ul className="space-y-1">
                {patch.relations.map((r, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2 text-xs">
                    {conceptLabel(r.from)}→
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
                    →{conceptLabel(r.to)}
                    <Code title="つなぐ列">{r.via}</Code>
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
            {draft.cleansing_exists ? '今の定義です。' : 'プロファイルから作った案です。'}
            次の 3 つを確かめてください（文字の列の前後の空白を除くことと、空文字を NULL にすることは、いつも行います）。確定すると{' '}
            <Code>warehouse/cleansing/{draft.source}.yml</Code> に置き、クレンジング層（stg）を生成します。
          </p>
        </CardHeader>
        <CardContent className="px-4">
          {spec ? (
            <div className="space-y-3">
              <CleansingForm
                spec={spec}
                onSpec={onSpec}
                profiles={profiles}
                kinds={draft.column_kinds ?? {}}
                dateNames={draft.date_derives ?? {}}
                labels={columnLabels}
              />
              <Button variant="ghost" size="sm" className="text-xs" onClick={onYaml}>
                ほかの設定（名前の変更・除外する行など）を YAML で直す（フォームで直した内容は捨てます）
              </Button>
            </div>
          ) : (
            <div className="space-y-1">
              {!draft.cleansing_spec && (
                <p className="text-xs text-amber-600">今の定義をフォームで読めなかったため、YAML で直してください。</p>
              )}
              <textarea
                aria-label="クレンジングの定義（YAML）"
                className="h-56 w-full rounded-md border bg-muted p-3 font-mono text-xs"
                value={cleansing}
                onChange={(e) => onCleansing(e.target.value)}
                spellCheck={false}
              />
            </div>
          )}
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

/** 英語の内部名（列名など）は、小さな補足として出す（#150） */
function Code({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <code className="font-mono text-[11px] text-muted-foreground" title={title}>
      {children}
    </code>
  )
}

const UNTRANSLATED = '日本語の表示名（未入力）'

/** 英語（ASCII）だけの表示名か（案の表示名が内部の名前のままのもの） */
function isUntranslated(label: string): boolean {
  return /^[\x20-\x7e]*$/.test(label)
}

/**
 * 案を受け取ったときに一度だけ、英語だけの表示名を空にする（日本語の表示名を入れてもらう。#150）。
 * 入力のたびには消さない（「ID」のような英字の表示名や、日本語入力の途中を壊さないため）。
 */
function blankUntranslated(patch: OntologyPatch): OntologyPatch {
  return {
    ...patch,
    concepts: patch.concepts.map((c) => ({
      ...c,
      label: isUntranslated(c.label) ? '' : c.label,
      attributes: c.attributes.map((a) => ({ ...a, label: isUntranslated(a.label) ? '' : a.label })),
    })),
  }
}

/** 確定のとき、空にした表示名は内部の名前で埋める（表示名は必須のため） */
function withFallbackLabels(patch: OntologyPatch): OntologyPatch {
  return {
    ...patch,
    concepts: patch.concepts.map((c) => ({
      ...c,
      label: c.label.trim() || c.name,
      attributes: c.attributes.map((a) => ({ ...a, label: a.label.trim() || a.column })),
    })),
  }
}

/** backend の adp.cleansing.check.column_kind の種類 → 表示 */
const KIND_LABELS: Record<string, string> = {
  text: '文字',
  timestamp: '日時',
  date: '日付',
  number: '数値',
  boolean: '真偽',
}

/** 属性の判断材料: 型と、範囲（数値・日付）かよく出る値（#150） */
function ValueHint({ profile: p, kind }: { profile?: ColumnProfile; kind?: string }) {
  if (!p) return <span className="text-muted-foreground">-</span>
  // 値の種類が多い数値・日付は範囲を、少ないもの（評価の 1〜5 など）はよく出る値を出す
  const ranged = !p.masked && p.min !== null && (p.histogram || p.monthly) && p.distinct_count > 10
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="secondary">{KIND_LABELS[kind ?? ''] ?? 'その他'}</Badge>
      {ranged ? (
        <span>
          {p.min} 〜 {p.max}
        </span>
      ) : (
        <TopValues profile={p} limit={4} />
      )}
    </div>
  )
}

function outputName(rule: CleansingRule): string {
  return rule.rename ?? rule.name
}

/**
 * クレンジングの定義をフォームで直す（#150）。確かめるのは、1 件を見分けるキー・区分の列に許す値・
 * 日時から日付を作るか。それ以外の項目（型の変換・除外など）は、そのまま残す。
 */
function CleansingForm({
  spec,
  onSpec,
  profiles,
  kinds,
  dateNames,
  labels,
}: {
  spec: CleansingSpec
  onSpec: (s: CleansingSpec) => void
  profiles: Map<string, ColumnProfile>
  kinds: Record<string, string>
  dateNames: Record<string, string | null>
  labels: Map<string, string>
}) {
  const rules = spec.columns ?? []
  const ruleOf = (column: string): CleansingRule => rules.find((r) => r.name === column) ?? { name: column }
  const setRule = (rule: CleansingRule) => {
    const exists = rules.some((r) => r.name === rule.name)
    onSpec({ ...spec, columns: exists ? rules.map((r) => (r.name === rule.name ? rule : r)) : [...rules, rule] })
  }
  const label = (column: string) => labels.get(column) ?? column
  const dropped = new Set(rules.filter((r) => r.drop).map((r) => r.name))
  // 定義に書ける列名（英小文字の snake_case）だけを選ばせる
  const columns = [...profiles.keys()].filter((c) => !dropped.has(c) && /^[a-z_][a-z0-9_]*$/.test(c))
  const keyOptions = columns.map((c) => [outputName(ruleOf(c)), c] as const)
  // 今のキーが選択肢に無くても（日付から作った列など）、表示と値を食い違わせない
  if (spec.key && !keyOptions.some(([out]) => out === spec.key)) keyOptions.push([spec.key, spec.key])
  const enums = rules.filter((r) => r.accepted_values !== undefined && !r.drop)
  const datetimes = Object.keys(dateNames)
  return (
    <div className="space-y-4 text-xs">
      <section className="space-y-1">
        <h3 className="font-semibold">1 件を見分けるキー</h3>
        <p className="text-muted-foreground">この列の値が重複・空になっていないかを、テストで確かめます。</p>
        <select
          aria-label="1 件を見分けるキー"
          className={SELECT}
          value={spec.key ?? ''}
          onChange={(e) => onSpec({ ...spec, key: e.target.value || null })}
        >
          <option value="">（なし）</option>
          {keyOptions.map(([out, column]) => (
            <option key={out} value={out}>
              {labels.has(column) ? `${label(column)}（${out}）` : out}
            </option>
          ))}
        </select>
      </section>
      <section className="space-y-1">
        <h3 className="font-semibold">区分の列に許す値</h3>
        <p className="text-muted-foreground">
          チェックした値以外が入っていたら、テストで止めます。数字は今のデータの行数です。
        </p>
        {enums.length === 0 && <p className="text-muted-foreground">区分の列はありません。</p>}
        {enums.map((r) => {
          const accepted = r.accepted_values ?? []
          const counts = new Map(
            (profiles.get(r.name)?.top_values ?? []).filter(([v]) => v !== null).map(([v, n]) => [v as string, n]),
          )
          const values = [...new Set([...accepted.map(String), ...counts.keys()])]
          // 数値の列は数値のまま書く（文字列と混ぜない）
          const typed = (v: string) => (kinds[r.name] === 'number' && !Number.isNaN(Number(v)) ? Number(v) : v)
          return (
            <div key={r.name} className="flex flex-wrap items-center gap-3" data-testid={`accepted-${r.name}`}>
              <span className="w-40 shrink-0">
                {label(r.name)} <Code>{r.name}</Code>
              </span>
              {values.map((v) => {
                const on = accepted.map(String).includes(v)
                return (
                  <label key={v} className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      aria-label={`${r.name} に許す値 ${v}`}
                      checked={on}
                      onChange={() =>
                        setRule({
                          ...r,
                          accepted_values: on ? accepted.filter((x) => String(x) !== v) : [...accepted, typed(v)],
                        })
                      }
                    />
                    {v}
                    {counts.has(v) && <span className="text-muted-foreground">{counts.get(v)?.toLocaleString()}</span>}
                  </label>
                )
              })}
              {accepted.length === 0 && (
                <span className="text-amber-600">どれも選ばないと、値を確かめるテストを作りません</span>
              )}
            </div>
          )
        })}
      </section>
      <section className="space-y-1">
        <h3 className="font-semibold">日時から日付を作るか</h3>
        <p className="text-muted-foreground">日付の列を作ると、日別・月別に集計しやすくなります。</p>
        {datetimes.length === 0 && <p className="text-muted-foreground">日時の列はありません。</p>}
        {datetimes.map((c) => {
          const rule = ruleOf(c)
          const derive = rule.derive ?? []
          const on = derive.some((d) => d.as === 'date')
          const name = dateNames[c]
          if (!name) {
            return (
              <p key={c} className="text-muted-foreground" data-testid={`derive-${c}`}>
                {label(c)} <Code>{c}</Code>: 決まりどおりの名前ではほかの列と重なるため、日付の列を作れません（YAML で名前を付けてください）
              </p>
            )
          }
          return (
            <label key={c} className="flex items-center gap-2" data-testid={`derive-${c}`}>
              <input
                type="checkbox"
                aria-label={`${c} から日付の列を作る`}
                checked={on}
                onChange={() => {
                  const rest = derive.filter((d) => d.as !== 'date')
                  // 表示名が無ければ説明を付けない（英語の列名を dbt の説明に入れない）
                  const added = labels.has(c)
                    ? { name, as: 'date' as const, description: `${label(c)}の日付` }
                    : { name, as: 'date' as const }
                  const next = on ? rest : [...rest, added]
                  const base: CleansingRule = { ...rule, derive: next }
                  if (!next.length) delete base.derive
                  setRule(base)
                }}
              />
              {label(c)} <Code>{c}</Code> から日付の列 <Code>{name}</Code> を作る
            </label>
          )
        })}
      </section>
    </div>
  )
}
