import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { exampleRequests, type Datasets } from '@/api/datasets'

import { DatasetsPanel } from './DatasetsPanel'

const datasets: Datasets = {
  metrics: [
    {
      name: 'revenue',
      label: '売上金額',
      description: '日別・店舗別の売上金額',
      type: 'simple',
      model: 'fct_order_lines',
      beam: 'sales_by_store',
      dimensions: [
        { name: 'store__channel', label: '販売チャネル', description: '', kind: 'categorical', entity: 'store' },
        { name: 'metric_time', label: '日付（指標の時間軸）', description: '', kind: 'time', entity: null },
      ],
    },
  ],
  models: [
    { name: 'fct_order_lines', kind: 'fct', description: '注文明細ファクト', columns: [] },
    { name: 'dim_store', kind: 'dim', description: '店舗ディメンション', columns: [{ name: 'channel', description: '販売チャネル' }] },
  ],
  sources: [
    {
      name: 'raw.returns',
      description: '返品',
      row_count: 1620,
      columns: [
        { name: 'quantity', description: '返品数量', data_type: 'INTEGER' },
        { name: 'return_reason', description: '返品理由', data_type: 'VARCHAR' },
        { name: 'returned_at', description: '返品日時', data_type: 'TIMESTAMP' },
      ],
    },
  ],
  beams: [],
  errors: [],
}

describe('DatasetsPanel', () => {
  it('既存のデータ（指標・fact・dimension）→ 源泉の順に表示し、検索で絞り込む', () => {
    render(<DatasetsPanel datasets={datasets} />)
    const order = ['指標（metrics）', 'fact', 'dimension'].map((n) => screen.getByRole('region', { name: n }))
    const source = screen.getByTestId('source-raw.returns')
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
    expect(order[2].compareDocumentPosition(source) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('metric-revenue')).toHaveTextContent('販売チャネル')
    expect(within(order[1]).getByText('fct_order_lines')).toBeInTheDocument()
    expect(within(order[2]).getByText('dim_store')).toBeInTheDocument()
    expect(source).toHaveTextContent('1,620 行・3 列')
    expect(within(source).getByRole('link', { name: /プロファイル/ })).toHaveAttribute('href', '?source=raw.returns')
    fireEvent.click(within(source).getByRole('button'))
    expect(source).toHaveTextContent('return_reason 返品理由')
    fireEvent.change(screen.getByLabelText('使えるデータを検索'), { target: { value: '返品' } })
    expect(screen.queryByTestId('metric-revenue')).not.toBeInTheDocument()
    expect(screen.queryByText('dim_store')).not.toBeInTheDocument()
    expect(screen.getByText('返品')).toBeInTheDocument()
  })

  it('インタビュー中（compact）はリンクを別タブで開く', () => {
    render(<DatasetsPanel datasets={datasets} compact />)
    expect(screen.getByRole('link', { name: /プロファイル/ })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: 'チャートで見る' })).toHaveAttribute('target', '_blank')
  })

  it('読み込みエラーを表示する', () => {
    render(<DatasetsPanel datasets={null} error="503" />)
    expect(screen.getByText(/読み込めませんでした/)).toBeInTheDocument()
  })
})

describe('exampleRequests', () => {
  it('日時や ID の列は切り口にしない', () => {
    expect(exampleRequests(datasets.sources)).toEqual(['返品理由別の返品の返品数量を見たい'])
  })
})

describe('exampleRequests の偏り', () => {
  it('テーブルを順に回して 1 件ずつ選ぶ', () => {
    const t = (name: string, desc: string) => ({
      name,
      description: desc,
      row_count: 1,
      columns: [
        { name: 'quantity', description: '数量', data_type: 'INTEGER' },
        { name: 'channel', description: 'チャネル', data_type: 'VARCHAR' },
        { name: 'status', description: 'ステータス', data_type: 'VARCHAR' },
      ],
    })
    const out = exampleRequests([t('raw.a', '注文'), t('raw.b', '返品')], 2)
    expect(out).toEqual(['チャネル別の注文の数量を見たい', 'チャネル別の返品の数量を見たい'])
  })
})

describe('rankDimensions', () => {
  it('時間 → よく使う区分 → その他の順にする', async () => {
    const { rankDimensions } = await import('@/api/datasets')
    const dims = [
      { name: 'customer__customer_name', kind: 'categorical' },
      { name: 'store__channel', kind: 'categorical' },
      { name: 'metric_time', kind: 'time' },
    ]
    expect(rankDimensions(dims).map((d) => d.name)).toEqual([
      'metric_time',
      'store__channel',
      'customer__customer_name',
    ])
  })
})

describe('errorText', () => {
  it('FastAPI の detail を取り出す', async () => {
    const { errorText } = await import('@/api/datasets')
    expect(errorText(new Error('{"detail":"データ一覧は利用できません"}'))).toBe('データ一覧は利用できません')
  })
})
