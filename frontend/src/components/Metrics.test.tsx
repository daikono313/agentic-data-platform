import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import { filterMetrics, groupDimensions, metricsUrl, type BusMatrix, type Datasets, type DimensionInfo } from '@/api/datasets'

import { MetricsScreen } from './Metrics'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

const dim = (name: string, label: string, kind = 'categorical'): DimensionInfo => ({
  name,
  label,
  description: '',
  kind,
  entity: name.includes('__') ? name.split('__')[0] : null,
})

/** 実際の semantic manifest と同じ形（dim は名前順、fact 自身の属性は後ろ） */
const bus: BusMatrix = {
  groups: [
    {
      name: 'dim_customer',
      label: '顧客',
      kind: 'dim',
      attributes: [
        { name: 'customer__region', label: '居住地方' },
        { name: 'customer__registered_on', label: '登録日' },
      ],
    },
    { name: 'dim_date', label: '日付', kind: 'dim', attributes: [{ name: 'date__calendar_month', label: '月' }] },
    { name: 'dim_product', label: '商品', kind: 'dim', attributes: [{ name: 'product__category', label: 'カテゴリ' }] },
    {
      name: 'dim_store',
      label: '店舗',
      kind: 'dim',
      attributes: [
        { name: 'store__store_name', label: '店舗名' },
        { name: 'store__channel', label: '販売チャネル' },
      ],
    },
    {
      name: 'fct_order_lines',
      label: '顧客が商品を注文する',
      kind: 'degenerate',
      attributes: [
        { name: 'order_line__ordered_on', label: '注文日' },
        { name: 'order_line__order_status', label: '注文ステータス' },
      ],
    },
  ],
  rows: [],
}

const dims = [
  dim('product__category', 'カテゴリ'),
  dim('store__store_name', '店舗名'),
  dim('metric_time', '日付（指標の時間軸）', 'time'),
  dim('order_line__order_status', '注文ステータス'),
  dim('order_line__ordered_on', '注文日', 'time'),
  dim('customer__registered_on', '登録日', 'time'),
  dim('date__calendar_month', '月'),
  dim('store__channel', '販売チャネル'),
  dim('customer__region', '居住地方'),
]

const summary = (d: DimensionInfo[], b?: BusMatrix) =>
  groupDimensions(d, b).map((g) => [g.label, g.dimensions.map((x) => x.label)])

describe('groupDimensions', () => {
  it('期間 → 日付 → バスマトリクスの列の順（dim → fact 自身の属性）。時間の切り口も持ち主の dimension に入る', () => {
    expect(summary(dims, bus)).toEqual([
      ['期間', ['日付（指標の時間軸）']],
      ['日付', ['月']],
      ['顧客', ['登録日', '居住地方']],
      ['商品', ['カテゴリ']],
      ['店舗', ['店舗名', '販売チャネル']],
      ['顧客が商品を注文する', ['注文日', '注文ステータス']],
    ])
  })

  it('どの指標でもグループの順は同じ（切り口の数によらない）', () => {
    const few = [dim('store__channel', '販売チャネル'), dim('product__category', 'カテゴリ'), dim('customer__region', '居住地方')]
    expect(groupDimensions(few, bus).map((g) => g.label)).toEqual(['顧客', '商品', '店舗'])
  })

  it('バスマトリクスに無い属性でも、同じ entity の dimension にまとめる', () => {
    expect(summary([dim('store__opened_on__month', '開店月', 'time'), dim('product__brand', 'ブランド')], bus)).toEqual([
      ['商品', ['ブランド']],
      ['店舗', ['開店月']],
    ])
  })

  it('role-playing（別名の entity）は、同じ属性名を持つ dimension の「（別の役割）」にまとめ、英語の名前を出さない', () => {
    const roles = [
      dim('store__store_name', '店舗名'),
      dim('ship_store__store_name', 'ship_store__store_name'),
      dim('order_date__calendar_month', 'order_date__calendar_month'),
      dim('ship_date__calendar_month', '出荷月'),
    ]
    const groups = groupDimensions(roles, bus)
    expect(groups.map((g) => [g.label, g.dimensions.map((x) => x.label)])).toEqual([
      ['日付（別の役割）', ['月']],
      ['日付（別の役割）', ['出荷月']],
      ['店舗', ['店舗名']],
      ['店舗（別の役割）', ['店舗名']],
    ])
    expect(new Set(groups.map((g) => g.key)).size).toBe(groups.length)
    expect(JSON.stringify(groups.map((g) => g.label))).not.toMatch(/[a-z]/)
  })

  it('バスマトリクスが無い・どこにも当たらない切り口は、時間なら「期間」、それ以外は「その他」（entity の英語名は出さない）', () => {
    expect(summary([dim('store__channel', '販売チャネル'), dim('channel', 'チャネル'), dim('order__ordered_on', '注文日', 'time')])).toEqual([
      ['期間', ['注文日']],
      ['その他', ['販売チャネル', 'チャネル']],
    ])
    expect(summary([dim('warehouse__name', '倉庫名')], bus)).toEqual([['その他', ['倉庫名']]])
  })
})

describe('metricsUrl / filterMetrics', () => {
  it('検索語を q で引き継ぐ', () => {
    expect(metricsUrl()).toBe('?metrics')
    expect(metricsUrl('  ')).toBe('?metrics')
    expect(metricsUrl('売上 & 店舗')).toBe('?metrics&q=%E5%A3%B2%E4%B8%8A+%26+%E5%BA%97%E8%88%97')
  })

  it('切り口のまとまりの見出しでも絞り込める', () => {
    const metrics = [{ name: 'm', label: '売上', description: '', type: 'simple', model: null, beam: null, dimensions: [dim('store__channel', '販売チャネル')] }]
    expect(filterMetrics(metrics, '店舗', bus)).toHaveLength(1)
    expect(filterMetrics(metrics, '店舗')).toHaveLength(0)
  })
})

const datasets: Datasets = {
  metrics: [
    { name: 'revenue', label: '売上金額', description: '店舗別の売上金額', type: 'simple', model: 'fct_order_lines', beam: null, dimensions: dims },
    { name: 'return_rate', label: '返品率', description: '返品数量 ÷ 販売数量', type: 'ratio', model: null, beam: null, dimensions: [] },
  ],
  models: [],
  bus,
  sources: [],
  beams: [],
  errors: ['チャートの定義を読めませんでした'],
}

describe('MetricsScreen', () => {
  it('指標ごとに説明・dimension ごとの切り口・チャートで見るを出し、検索で絞り込む', async () => {
    vi.spyOn(api, 'datasets').mockResolvedValue(datasets)
    render(<MetricsScreen />)
    const revenue = await screen.findByTestId('metric-revenue')
    expect(screen.getByRole('heading', { level: 1 })).toHaveAccessibleName('指標の一覧')
    expect(screen.getByTestId('metrics-count')).toHaveTextContent('2 / 2 件')
    expect(screen.getByText(/⚠ チャートの定義を読めませんでした/)).toBeInTheDocument()
    expect(revenue).toHaveTextContent('店舗別の売上金額')
    expect(within(revenue).getByRole('group', { name: '店舗' })).toHaveTextContent('店舗名・販売チャネル')
    expect(within(revenue).getByRole('group', { name: '期間' })).toHaveTextContent('日付（指標の時間軸）')
    expect(within(revenue).getByRole('link', { name: 'チャートで見る' })).toHaveAttribute('href', expect.stringContaining('revenue'))
    expect(screen.getByRole('link', { name: /ホーム/ })).toHaveAttribute('href', '/')

    // 切り口の表示名でも絞り込める
    fireEvent.change(screen.getByLabelText('指標を検索'), { target: { value: '居住地方' } })
    expect(screen.getByTestId('metric-revenue')).toBeInTheDocument()
    expect(screen.queryByTestId('metric-return_rate')).not.toBeInTheDocument()
    expect(screen.getByTestId('metrics-count')).toHaveTextContent('1 / 2 件')
    fireEvent.change(screen.getByLabelText('指標を検索'), { target: { value: '該当なし' } })
    expect(screen.getByText('検索に合う指標はありません。')).toBeInTheDocument()
  })

  it('URL の q を検索語の初期値にする', async () => {
    window.history.replaceState(null, '', '/?metrics&q=%E8%BF%94%E5%93%81')
    vi.spyOn(api, 'datasets').mockResolvedValue(datasets)
    render(<MetricsScreen />)
    expect(await screen.findByTestId('metric-return_rate')).toBeInTheDocument()
    expect(screen.getByLabelText('指標を検索')).toHaveValue('返品')
    expect(screen.queryByTestId('metric-revenue')).not.toBeInTheDocument()
  })

  it('読み込みに失敗したらエラーを出す', async () => {
    vi.spyOn(api, 'datasets').mockRejectedValue(new Error('{"detail":"manifest が無い"}'))
    render(<MetricsScreen />)
    expect(await screen.findByText(/指標を読み込めませんでした: manifest が無い/)).toBeInTheDocument()
  })
})
