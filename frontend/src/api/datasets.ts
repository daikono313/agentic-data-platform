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
}
export interface BeamInfo {
  name: string
  title: string
  request: string
  metrics: string[]
}
export interface Datasets {
  metrics: MetricInfo[]
  models: ModelInfo[]
  sources: SourceInfo[]
  beams: BeamInfo[]
  errors: string[]
}

const label = (c: ColumnInfo) => (c.description || c.name).split(/[（(。]/)[0].trim()
const isNumeric = (c: ColumnInfo) => /INT|DOUBLE|DECIMAL|FLOAT|NUMERIC|REAL/i.test(c.data_type ?? '')
const isText = (c: ColumnInfo) => !c.data_type || /VARCHAR|TEXT|STRING/i.test(c.data_type)

/**
 * 元データの列から「作れそうな指標」の要求の例を作る。
 * 金額・数量の列と、区分の列（〜ID・日時以外）を組み合わせる。
 */
export function exampleRequests(sources: SourceInfo[], limit = 4): string[] {
  const measures: { table: SourceInfo; col: ColumnInfo }[] = []
  const categories: ColumnInfo[] = []
  for (const s of sources) {
    for (const c of s.columns) {
      // 金額・数量（数値の列）と、区分（文字列の列）。ID・日時は使わない
      if (/(amount|price|quantity|qty)$/.test(c.name) && (isNumeric(c) || !c.data_type)) measures.push({ table: s, col: c })
      else if (
        isText(c) &&
        !/(_id|_at|_on|_date|_name)$/.test(c.name) &&
        /(category|channel|region|rank|method|type|reason|status)$/.test(c.name)
      )
        categories.push(c)
    }
  }
  const out: string[] = []
  for (const m of measures) {
    for (const c of categories) {
      const text = `${label(c)}別の${label(m.col)}を見たい`
      if (!out.includes(text)) out.push(text)
      if (out.length >= limit) return out
    }
  }
  return out
}

export function matches(query: string, ...texts: string[]): boolean {
  const q = query.trim().toLowerCase()
  return !q || texts.some((t) => t.toLowerCase().includes(q))
}
