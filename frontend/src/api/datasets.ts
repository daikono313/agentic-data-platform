/** GET /datasets の型（backend の adp.datasets と対応） */
export interface DimensionInfo {
  name: string
  label: string
  description: string
  kind: 'time' | 'categorical' | string
  entity: string | null
}
export interface MetricInfo {
  name: string
  label: string
  description: string
  type: string
  model: string | null
  dimensions: DimensionInfo[]
  beam: string | null
}
export interface ColumnInfo {
  name: string
  description: string
  data_type?: string
}
export interface ModelInfo {
  name: string
  kind: 'fct' | 'dim' | string
  description: string
  columns: ColumnInfo[]
}
export interface SourceInfo {
  name: string
  description: string
  row_count: number
  columns: ColumnInfo[]
  /** オントロジーのどの概念にもなっていない（未整理。取り込みが必要。#69） */
  unmapped?: boolean
  /** この源泉を使っている fact / dim（空なら未活用。null は分からない（dbt の manifest が無い）。#72） */
  used_by?: string[] | null
}
export interface BeamInfo {
  name: string
  title: string
  request: string
  metrics: string[]
}
/** GET /sources/{table}/profile の型（backend の adp.datasets.SourceProfile と対応） */
export interface ColumnProfile {
  column: string
  data_type: string
  row_count: number
  null_count: number
  distinct_count: number
  min: string | null
  max: string | null
  top_values: [string | null, number][]
  is_unique: boolean
  /** 日付・日時の列の月ごとの件数（件数 0 の月も含む。#71） */
  monthly?: [string, number][] | null
  /** 数値の列の分布（下限・上限・件数） */
  histogram?: [number, number, number][] | null
}
export interface SourceProfile {
  schema_name: string
  name: string
  description: string
  row_count: number
  columns: ColumnInfo[]
  primary_key_candidates: string[]
  foreign_key_candidates: { column: string; references: string; match_ratio: number }[]
  profiles: ColumnProfile[]
  used_by: string[]
}

/** GET /ontology の型（backend の adp.datasets.OntologyView と対応。#67） */
export interface OntologyConcept {
  name: string
  label: string
  domain: string
  kind: 'entity' | 'event'
  source: string
  key: string
  model?: string
  attributes: { name: string; label: string; column: string }[]
}
export interface OntologyView {
  mermaid: string
  ontology: {
    domains: { name: string; label: string }[]
    concepts: OntologyConcept[]
    relations: { from: string; to: string; verb: string; via: string }[]
    constraints: { concept: string; text: string }[]
  }
  unmapped_sources: string[]
  unmapped_models: string[]
  errors: string[]
}

/** GET /sources/{table}/cleansing の型（#68） */
export interface CleansingView {
  path: string
  existing: string | null
  draft: string | null
  draft_error?: string | null
}

/** 源泉の取り込み（#69） */
export interface OntologyPatch {
  domains: { name: string; label: string }[]
  concepts: OntologyConcept[]
  relations: { from: string; to: string; verb: string; via: string }[]
  constraints: { concept: string; text: string }[]
}
export interface OnboardingDraft {
  source: string
  patch: OntologyPatch
  proposer: 'agent' | 'profile'
  note: string | null
  cleansing: string
  cleansing_exists: boolean
  mermaid: string
  bus: BusMatrix
}

export function onboardUrl(table: string): string {
  return `?${new URLSearchParams({ onboard: table }).toString()}`
}

export function sourceUrl(table: string): string {
  return `?${new URLSearchParams({ source: table }).toString()}`
}

/** バスマトリクス（fact × dimension。#62） */
export interface BusGroup {
  name: string
  label: string
  kind: 'dim' | 'degenerate' | string
  attributes: { name: string; label: string }[]
  /** 元の源泉（#72） */
  sources?: string[]
  /** 源泉の取り込みで増える候補（#69） */
  candidate?: boolean
}
export interface BusRow {
  name: string
  label: string
  groups: string[]
  /** 元の源泉（#72） */
  sources?: string[]
  /** 源泉の取り込みで増える候補（#69） */
  candidate?: boolean
}
export interface BusMatrix {
  groups: BusGroup[]
  rows: BusRow[]
}

export interface Datasets {
  metrics: MetricInfo[]
  models: ModelInfo[]
  bus?: BusMatrix
  sources: SourceInfo[]
  beams: BeamInfo[]
  errors: string[]
}

const label = (c: ColumnInfo) => (c.description || c.name).split(/[（(。]/)[0].trim()
const isNumeric = (c: ColumnInfo) => /INT|DOUBLE|DECIMAL|FLOAT|NUMERIC|REAL/i.test(c.data_type ?? '')
const isText = (c: ColumnInfo) => !c.data_type || /VARCHAR|TEXT|STRING/i.test(c.data_type)

/**
 * 元データの列から「作れそうな指標」の要求の例を作る。
 * 金額・数量の列と、同じテーブルの区分の列（ID・日時・名前以外の文字列）を組み合わせる。
 * 1 つのテーブルに偏らないよう、テーブルを順に回して 1 件ずつ選ぶ。
 */
export function exampleRequests(sources: SourceInfo[], limit = 4): string[] {
  const perTable: string[][] = sources.map((s) => {
    const measures = s.columns.filter(
      (c) => /(amount|price|quantity|qty)$/.test(c.name) && (isNumeric(c) || !c.data_type),
    )
    const categories = s.columns.filter(
      (c) =>
        isText(c) &&
        !/(_id|_at|_on|_date|_name)$/.test(c.name) &&
        /(category|channel|region|rank|method|type|reason|status)$/.test(c.name),
    )
    const table = (s.description || s.name).split(/[（(。]/)[0].trim()
    return measures.flatMap((m) => categories.map((c) => `${label(c)}別の${table}の${label(m)}を見たい`))
  })
  const out: string[] = []
  for (let round = 0; out.length < limit && perTable.some((t) => t.length > round); round++) {
    for (const t of perTable) {
      if (t[round] && !out.includes(t[round])) out.push(t[round])
      if (out.length >= limit) break
    }
  }
  return out
}

const PRIORITY = /(channel|category|region|store_name|rank|type|status|month|year|reason|method)$/

/** 切り口の表示順: 時間 → よく使う区分（チャネル・カテゴリ・地域・店舗名など）→ その他 */
export function rankDimensions<T extends { name: string; kind: string }>(dims: T[]): T[] {
  const score = (d: T) => (d.kind === 'time' ? 0 : PRIORITY.test(d.name) ? 1 : 2)
  return [...dims].sort((a, b) => score(a) - score(b))
}

/** API のエラー（FastAPI の {"detail": ...}）を読みやすい文にする */
export function errorText(e: unknown): string {
  const text = e instanceof Error ? e.message : String(e)
  try {
    const detail = (JSON.parse(text) as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
  } catch {
    // JSON でなければそのまま
  }
  return text
}

export function matches(query: string, ...texts: string[]): boolean {
  const q = query.trim().toLowerCase()
  return !q || texts.some((t) => t.toLowerCase().includes(q))
}

const STOP = /(を|が|は|に|で|と|ごと|見たい|知りたい|みたい|したい|ほしい|欲しい|毎|[、。・\s「」（）()])/g

/** 単独では「同じものを測る」根拠にならない言葉（返品数量と販売数量、返品金額と売上金額は別の指標） */
const GENERIC = new Set(['数量', '金額', '件数', '個数', '人数', '合計', '税抜', '税込'])

function bigrams(text: string): Set<string> {
  const out = new Set<string>()
  for (const word of text.replace(STOP, ' ').split(' ')) {
    for (let i = 0; i < word.length - 1; i++) out.add(word.slice(i, i + 2))
  }
  return out
}

/** 表示名のうち、何を測るかの部分（「商品別販売数量」→「販売数量」。「◯◯別」は切り口） */
function measurePart(label: string): string {
  const i = label.lastIndexOf('別')
  return i >= 0 ? label.slice(i + 1) : label
}

/**
 * 要求の文に似た既にある指標（#75）。指標の表示名のうち何を測るかの部分と、要求の 2 文字の並びが
 * 重なるものを、重なりの多い順に出す。切り口（「◯◯別」）・説明（「除く」などの条件を含む）は
 * 比べない。数量・金額のような汎用の言葉だけが重なるものは出さない。
 */
export function similarMetrics(request: string, metrics: MetricInfo[], limit = 3): MetricInfo[] {
  const want = bigrams(request.replace(/別/g, ' '))
  if (want.size === 0) return []
  return metrics
    .map((m) => {
      const hits = [...bigrams(measurePart(m.label))].filter((b) => want.has(b))
      return { m, hits: hits.length, specific: hits.some((b) => !GENERIC.has(b)) }
    })
    .filter((x) => x.specific)
    .sort((a, b) => b.hits - a.hits)
    .slice(0, limit)
    .map((x) => x.m)
}
