import type { MetricInfo } from './datasets'

/** GET /charts/options・POST /charts/render の型（backend の adp.chart_view と対応） */
export type Grain = 'day' | 'week' | 'month' | 'quarter' | 'year'

export interface ChartOptions {
  metrics: MetricInfo[]
  errors: string[]
  /** 生成 PR の内容（マージ前）で見ている */
  preview: boolean
}

export interface ChartRequest {
  metric: string
  dimension: string | null
  grain: Grain | null
  start: string | null
  end: string | null
}

export interface ChartResult {
  svg: string
  kind: 'line' | 'bar' | 'kpi' | string
  title: string
  sql: string
}

export const GRAINS: { value: Grain | null; label: string }[] = [
  { value: 'day', label: '日' },
  { value: 'week', label: '週' },
  { value: 'month', label: '月' },
  { value: 'quarter', label: '四半期' },
  { value: 'year', label: '年' },
  { value: null, label: '時間で分けない' },
]

/** SVG は <img> で表示する（スクリプトを実行しない） */
export function svgDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

/** 描画画面の URL（?chart=<指標>&job=<生成ジョブ>） */
export function chartUrl(metric: string, jobId?: string | null, dimension?: string | null): string {
  const params = new URLSearchParams({ chart: metric })
  if (jobId) params.set('job', jobId)
  if (dimension) params.set('dimension', dimension)
  return `?${params.toString()}`
}
