import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

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
  models: [{ name: 'fct_order_lines', kind: 'fct', description: '注文明細ファクト', columns: [] }],
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
  it('指標・モデル・元データを表示し、検索で絞り込む', () => {
    render(<DatasetsPanel datasets={datasets} />)
    expect(screen.getByTestId('metric-revenue')).toHaveTextContent('販売チャネル')
    expect(screen.getByText('fct_order_lines')).toBeInTheDocument()
    expect(screen.getByText('返品')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('使えるデータを検索'), { target: { value: '返品' } })
    expect(screen.queryByTestId('metric-revenue')).not.toBeInTheDocument()
    expect(screen.getByText('返品')).toBeInTheDocument()
  })

  it('要求の例をクリックすると入力欄に渡す', () => {
    const pick = vi.fn()
    render(<DatasetsPanel datasets={datasets} onPickExample={pick} />)
    fireEvent.click(screen.getByRole('button', { name: '返品理由別の返品数量を見たい' }))
    expect(pick).toHaveBeenCalledWith('返品理由別の返品数量を見たい')
  })

  it('読み込みエラーを表示する', () => {
    render(<DatasetsPanel datasets={null} error="503" />)
    expect(screen.getByText(/読み込めませんでした/)).toBeInTheDocument()
  })
})

describe('exampleRequests', () => {
  it('日時や ID の列は切り口にしない', () => {
    expect(exampleRequests(datasets.sources)).toEqual(['返品理由別の返品数量を見たい'])
  })
})
