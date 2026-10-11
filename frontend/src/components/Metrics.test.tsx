import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import { groupDimensions, type Datasets, type DimensionInfo } from '@/api/datasets'

import { MetricsScreen } from './Metrics'

afterEach(() => vi.restoreAllMocks())

const dim = (name: string, label: string, kind = 'categorical'): DimensionInfo => ({
  name,
  label,
  description: '',
  kind,
  entity: name.includes('__') ? name.split('__')[0] : null,
})

const dims = [
  dim('product__product_name', '商品名'),
  dim('store__store_name', '店舗名'),
  dim('metric_time', '日付', 'time'),
  dim('store__channel', '販売チャネル'),
  dim('order__status', '注文状態'),
  dim('product__category', 'カテゴリ'),
  dim('store__region', '地域'),
]

const datasets: Datasets = {
  metrics: [
    { name: 'revenue', label: '売上金額', description: '店舗別の売上金額', type: 'simple', model: 'fct_sales', beam: null, dimensions: dims },
    { name: 'return_rate', label: '返品率', description: '返品数量 ÷ 販売数量', type: 'ratio', model: null, beam: null, dimensions: [] },
  ],
  models: [],
  bus: {
    groups: [
      { name: 'dim_store', label: '店舗', kind: 'dim', attributes: [{ name: 'store__store_name', label: '店舗名' }, { name: 'store__channel', label: '販売チャネル' }, { name: 'store__region', label: '地域' }] },
      { name: 'dim_product', label: '商品', kind: 'dim', attributes: [{ name: 'product__product_name', label: '商品名' }, { name: 'product__category', label: 'カテゴリ' }] },
    ],
    rows: [],
  },
  sources: [],
  beams: [],
  errors: [],
}

describe('groupDimensions', () => {
  it('期間を先頭に、dimension ごと（多い順）にまとめ、表示名はバスマトリクスから取る', () => {
    const groups = groupDimensions(dims, datasets.bus)
    expect(groups.map((g) => [g.label, g.dimensions.map((d) => d.label)])).toEqual([
      ['期間', ['日付']],
      ['店舗', ['店舗名', '販売チャネル', '地域']],
      ['商品', ['カテゴリ', '商品名']],
      ['order', ['注文状態']],
    ])
  })

  it('バスマトリクスが無ければ entity の名前でまとめ、entity の無い切り口は「その他」', () => {
    const groups = groupDimensions([dim('store__channel', '販売チャネル'), dim('channel', 'チャネル')])
    expect(groups.map((g) => g.label)).toEqual(['store', 'その他'])
  })
})

describe('MetricsScreen', () => {
  it('指標ごとに説明・dimension ごとの切り口・チャートで見るを出し、検索で絞り込む', async () => {
    vi.spyOn(api, 'datasets').mockResolvedValue(datasets)
    render(<MetricsScreen />)
    const revenue = await screen.findByTestId('metric-revenue')
    expect(screen.getByRole('heading', { name: /指標の一覧/ })).toHaveTextContent('2 件')
    expect(revenue).toHaveTextContent('店舗別の売上金額')
    expect(within(revenue).getByTestId('dimension-group-dim_store')).toHaveTextContent('店舗店舗名・販売チャネル・地域')
    expect(within(revenue).getByTestId('dimension-group-__time')).toHaveTextContent('期間日付')
    expect(within(revenue).getByRole('link', { name: 'チャートで見る' })).toHaveAttribute('href', expect.stringContaining('revenue'))
    expect(screen.getByRole('link', { name: /ホーム/ })).toHaveAttribute('href', '/')

    // 切り口の表示名でも絞り込める
    fireEvent.change(screen.getByLabelText('指標を検索'), { target: { value: '地域' } })
    expect(screen.getByTestId('metric-revenue')).toBeInTheDocument()
    expect(screen.queryByTestId('metric-return_rate')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('指標を検索'), { target: { value: '該当なし' } })
    expect(screen.getByText('条件に合う指標がありません。')).toBeInTheDocument()
  })

  it('読み込みに失敗したらエラーを出す', async () => {
    vi.spyOn(api, 'datasets').mockRejectedValue(new Error('{"detail":"manifest が無い"}'))
    render(<MetricsScreen />)
    expect(await screen.findByText(/指標を読み込めませんでした: manifest が無い/)).toBeInTheDocument()
  })
})
