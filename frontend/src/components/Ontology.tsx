import { ArrowLeft, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api, errorText } from '@/api/client'
import { KIND_LABELS, onboardUrl, sourceUrl, type OntologyView } from '@/api/datasets'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

let queue: Promise<unknown> = Promise.resolve()
let seq = 0

/** mermaid の描画は同時に走らせると壊れる（一時的な DOM を共有する）ので、1 つずつ順に行う */
function renderMermaid(text: string): Promise<string> {
  const run = queue.then(async () => {
    const { default: mermaid } = await import('mermaid')
    const dark = window.matchMedia?.('(prefers-color-scheme: dark)').matches
    mermaid.initialize({
      startOnLoad: false,
      // ラベル中の HTML・スクリプトを無効にする
      securityLevel: 'strict',
      theme: dark ? 'dark' : 'default',
      // HTML のラベル（foreignObject）はページの CSS の影響で幅の計算がずれて文字が切れるので、SVG の文字にする
      flowchart: { htmlLabels: false },
    })
    const { svg } = await mermaid.render(`mermaid-${++seq}`, text)
    return svg
  })
  queue = run.catch(() => undefined)
  return run
}

/** Mermaid の図を描く。mermaid は大きいので、この画面を開いたときだけ読み込む */
export function MermaidDiagram({ text }: { text: string }) {
  const [svg, setSvg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    renderMermaid(text)
      .then((s) => !cancelled && setSvg(s))
      .catch((e) => !cancelled && setError(errorText(e)))
    return () => {
      cancelled = true
    }
  }, [text])
  if (error) return <p className="text-sm text-destructive">図を描けませんでした: {error}</p>
  if (!svg) return <p className="text-xs text-muted-foreground">図を描いています…</p>
  // svg は mermaid が securityLevel: strict で生成したもの（ラベルは文字として扱われる）
  return (
    <div
      className="overflow-x-auto [&_svg]:mx-auto [&_svg]:max-w-none"
      data-testid="mermaid"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

/** オントロジー（業務の概念・属性・関係・制約）の画面（#67） */
export function OntologyScreen() {
  const [view, setView] = useState<OntologyView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api
      .ontology()
      .then(setView)
      .catch((e) => setError(errorText(e)))
  }, [])

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <a href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="size-4" /> ホーム
      </a>
      <header>
        <h1 className="text-xl font-bold">オントロジー</h1>
        <p className="text-sm text-muted-foreground">
          業務の概念と、その属性・関係・制約。源泉が増えたら、まずここを更新してから fact / dim を整理します。正本は{' '}
          <code className="font-mono text-xs">ontology/ontology.yml</code>
        </p>
      </header>
      {error && <p className="text-sm text-destructive">オントロジーを読み込めませんでした: {error}</p>}
      {!view && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> 読み込んでいます…
        </p>
      )}
      {view && <OntologyBody view={view} />}
    </main>
  )
}

function OntologyBody({ view }: { view: OntologyView }) {
  const { ontology } = view
  const domain = new Map(ontology.domains.map((d) => [d.name, d.label]))
  const label = new Map(ontology.concepts.map((c) => [c.name, c.label]))
  return (
    <>
      {(view.errors.length > 0 || view.unmapped_sources.length > 0 || view.unmapped_models.length > 0) && (
        <Card className="gap-1 border-amber-500/60 py-3" aria-label="食い違い">
          <CardContent className="space-y-1 px-4 text-xs">
            {view.errors.map((e, i) => (
              <p key={i} className="text-destructive">
                ✗ {e}
              </p>
            ))}
            {view.unmapped_models.length > 0 && (
              <p className="text-muted-foreground">概念を持たない共通のモデル: {view.unmapped_models.join('、')}</p>
            )}
            {view.unmapped_sources.length > 0 && (
              <p>
                未整理の源泉（どの概念にもなっていない）:{' '}
                {view.unmapped_sources.map((s) => (
                  <a key={s} href={onboardUrl(s)} className="mr-2 font-mono text-primary underline">
                    {s}（取り込む）
                  </a>
                ))}
              </p>
            )}
          </CardContent>
        </Card>
      )}
      <Card className="gap-2 py-3">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">概念の関係</CardTitle>
          <p className="text-xs text-muted-foreground">
            四角 = もの・人・場所（dim の候補）、長円 = 出来事（fact の候補）、六角形 = 状態（定期的に測る値。fact の候補）、灰色 = 属性（一部）、緑 = 制約、矢印 = 関係
          </p>
        </CardHeader>
        <CardContent className="px-4">
          <MermaidDiagram text={view.mermaid} />
        </CardContent>
      </Card>
      <Card className="gap-2 py-3">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">概念</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>概念</TableHead>
                <TableHead>種類</TableHead>
                <TableHead>ドメイン</TableHead>
                <TableHead>源泉</TableHead>
                <TableHead>モデル</TableHead>
                <TableHead>属性</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ontology.concepts.map((c) => (
                <TableRow key={c.name} data-testid={`concept-${c.name}`} className="align-top">
                  <TableCell>
                    <p className="font-medium">{c.label}</p>
                    <p className="font-mono text-xs text-muted-foreground">{c.name}</p>
                  </TableCell>
                  <TableCell className="text-xs">{KIND_LABELS[c.kind]}</TableCell>
                  <TableCell className="text-xs">{domain.get(c.domain)}</TableCell>
                  <TableCell className="text-xs">
                    <a href={sourceUrl(c.source)} className="font-mono text-primary underline">
                      {c.source}
                    </a>
                  </TableCell>
                  <TableCell className="text-xs">
                    {c.model ? <span className="font-mono">{c.model}</span> : <Badge variant="outline">未整備</Badge>}
                  </TableCell>
                  <TableCell className="text-xs whitespace-normal">{c.attributes
                      .map((a) => (a.synonyms?.length ? `${a.label}（同義語: ${a.synonyms.join('・')}）` : a.label))
                      .join('、')}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      {ontology.relations.length > 0 && (
        <Card className="gap-2 py-3">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">関係</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <Table aria-label="関係">
              <TableHeader>
                <TableRow>
                  <TableHead>から</TableHead>
                  <TableHead>関係</TableHead>
                  <TableHead>へ</TableHead>
                  <TableHead>列</TableHead>
                  <TableHead>多重度</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {ontology.relations.map((r, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-xs">{label.get(r.from)}</TableCell>
                    <TableCell className="text-xs font-medium">{r.verb}</TableCell>
                    <TableCell className="text-xs">{label.get(r.to)}</TableCell>
                    <TableCell className="font-mono text-xs">{r.via}</TableCell>
                    <TableCell className="text-xs whitespace-normal">{view.multiplicities[i] ?? '-'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
      {ontology.constraints.length > 0 && (
        <Card className="gap-2 py-3">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">制約</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <ul className="list-disc space-y-0.5 pl-5 text-xs">
              {ontology.constraints.map((x, i) => (
                <li key={i}>
                  <span className="font-medium">{label.get(x.concept)}</span>: {x.text}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </>
  )
}
