import { fireEvent, render, screen, within } from '@testing-library/react'
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
    // 開始画面の指標は表示名と切り口のまとまり（dimension）だけ。すべては ?metrics で（#107）
    // バスマトリクスが無いので entity 名は出さず「その他」。「期間」は一行の要約には出さない
    expect(screen.getByTestId('metric-revenue')).toHaveTextContent(/^売上金額その他チャートで見る$/)
    expect(screen.getByRole('link', { name: /すべての指標/ })).toHaveAttribute('href', '?metrics')
    expect(within(order[1]).getByText('fct_order_lines')).toBeInTheDocument()
    expect(within(order[2]).getByText('dim_store')).toBeInTheDocument()
    expect(source).toHaveTextContent('1,620 行・3 列')
    expect(within(source).getByRole('link', { name: /プロファイル/ })).toHaveAttribute('href', '?source=raw.returns')
    fireEvent.click(within(source).getByRole('button'))
    expect(source).toHaveTextContent('return_reason 返品理由')
    fireEvent.change(screen.getByLabelText('使えるデータを検索'), { target: { value: '返品' } })
    expect(screen.queryByTestId('metric-revenue')).not.toBeInTheDocument()
    expect(screen.getByText('検索に合う指標はありません。')).toBeInTheDocument()
    expect(screen.queryByText('まだありません。要求すると作れます。')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /すべての指標/ })).toHaveAttribute('href', '?metrics&q=%E8%BF%94%E5%93%81')
    expect(screen.queryByText('dim_store')).not.toBeInTheDocument()
    expect(screen.getByText('返品')).toBeInTheDocument()
  })

  it('開始画面には代表の数件だけを出し、残りの件数と指標の一覧へのリンクを出す（#107）', () => {
    const metrics = Array.from({ length: 6 }, (_, i) => ({ ...datasets.metrics[0], name: `m${i}`, label: `指標${i}` }))
    render(<DatasetsPanel datasets={{ ...datasets, metrics }} />)
    const section = screen.getByRole('region', { name: '指標（metrics）' })
    expect(section).toHaveTextContent('6')
    expect(within(section).getAllByRole('listitem')).toHaveLength(4)
    expect(within(section).getByRole('link', { name: /ほか 2 件/ })).toHaveAttribute('href', '?metrics')
    // 検索は指標にも効く
    fireEvent.change(screen.getByLabelText('使えるデータを検索'), { target: { value: '指標5' } })
    expect(within(section).getAllByRole('listitem')).toHaveLength(1)
    expect(screen.getByTestId('metric-m5')).toBeInTheDocument()
  })

  it('源泉ごとに使っている fact / dim と、未活用・未整理の数を出す（#72）', () => {
    const base = datasets.sources[0]
    const sources = [
      { ...base, name: 'raw.orders', used_by: ['fct_order_lines'] },
      { ...base, name: 'raw.returns', used_by: [] },
      { ...base, name: 'raw.inventory', used_by: [], unmapped: true },
    ]
    render(<DatasetsPanel datasets={{ ...datasets, sources }} />)
    expect(screen.getByTestId('source-coverage')).toHaveTextContent('fact / dim に使われている 1・未活用 1・未整理（オントロジーに無い） 1')
    expect(screen.getByTestId('source-raw.orders')).toHaveTextContent('fct_order_lines')
    expect(screen.getByTestId('source-raw.returns')).toHaveTextContent('未活用')
    expect(screen.getByTestId('source-raw.inventory')).not.toHaveTextContent('未活用（')
  })

  it('使われているか分からない（manifest が無い）ときは、未活用と言わない', () => {
    const sources = [{ ...datasets.sources[0], used_by: null }]
    render(<DatasetsPanel datasets={{ ...datasets, sources }} />)
    expect(screen.queryByTestId('source-coverage')).not.toBeInTheDocument()
    expect(screen.getByTestId('source-raw.returns')).not.toHaveTextContent('未活用')
  })

  it('バスマトリクスの fact に元の源泉を出す', () => {
    const bus = {
      groups: [{ name: 'dim_store', label: '店舗', kind: 'dim', attributes: [{ name: 'store__channel', label: 'チャネル' }], sources: ['raw.stores'] }],
      rows: [{ name: 'fct_order_lines', label: '注文', groups: ['dim_store'], sources: ['raw.order_items', 'raw.orders'] }],
    }
    render(<DatasetsPanel datasets={{ ...datasets, bus }} />)
    expect(screen.getByTestId('bus-fct_order_lines')).toHaveTextContent('← raw.order_items, raw.orders')
    expect(screen.getByRole('columnheader', { name: /店舗/ })).toHaveAttribute('title', expect.stringContaining('元の源泉: raw.stores'))
  })

  it('未整理の源泉には取り込みへのリンクを出す', () => {
    const sources = [{ ...datasets.sources[0], unmapped: true }]
    render(<DatasetsPanel datasets={{ ...datasets, sources }} />)
    expect(screen.getByRole('link', { name: /未整理.*取り込む/ })).toHaveAttribute('href', '?onboard=raw.returns')
  })

  it('インタビュー中（compact）はリンクを別タブで開く', () => {
    render(<DatasetsPanel datasets={datasets} compact />)
    expect(screen.getByRole('link', { name: /プロファイル/ })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: 'チャートで見る（別のタブ）' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: /すべての指標.*（別のタブ）/ })).toHaveAttribute('target', '_blank')
  })

  it('compact では代表の 3 件だけを出し、指標の一覧は別のタブで開く（#107）', () => {
    const metrics = Array.from({ length: 5 }, (_, i) => ({ ...datasets.metrics[0], name: `m${i}`, label: `指標${i}` }))
    render(<DatasetsPanel datasets={{ ...datasets, metrics }} compact />)
    const section = screen.getByRole('region', { name: '指標（metrics）' })
    expect(within(section).getAllByRole('listitem')).toHaveLength(3)
    const link = within(section).getByRole('link', { name: 'ほか 2 件。すべての指標（説明・切り口）を見る（別のタブ）' })
    expect(link).toHaveAttribute('href', '?metrics')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('読み込みエラーを表示する', () => {
    render(<DatasetsPanel datasets={null} error="503" />)
    expect(screen.getByText(/読み込めませんでした/)).toBeInTheDocument()
  })
})

describe('バスマトリクス', () => {
  const bus = {
    groups: [
      { name: 'dim_store', label: '店舗', kind: 'dim', attributes: [{ name: 'store__channel', label: '販売チャネル' }] },
      { name: 'dim_promotion', label: 'プロモーション', kind: 'dim', attributes: [{ name: 'promotion__type', label: '種類' }] },
      { name: 'fct_order_lines', label: '注文', kind: 'degenerate', attributes: [{ name: 'order_line__status', label: '注文ステータス' }] },
    ],
    rows: [{ name: 'fct_order_lines', label: '注文', groups: ['dim_store', 'fct_order_lines'] }],
  }

  it('fact × dimension の表を出し、結合できる属性に ✓ を付ける', () => {
    const extra = { name: 'dim_unregistered', kind: 'dim', description: '未登録', columns: [] }
    render(<DatasetsPanel datasets={{ ...datasets, models: [...datasets.models, extra], bus }} />)
    const table = screen.getByRole('table', { name: 'バスマトリクス' })
    expect(within(table).getByRole('columnheader', { name: /店舗/ })).toHaveAttribute('colspan', '1')
    expect(within(table).getByRole('columnheader', { name: '販売チャネル' })).toBeInTheDocument()
    const row = screen.getByTestId('bus-fct_order_lines')
    expect(within(row).getByLabelText('fct_order_lines × 販売チャネル')).toBeInTheDocument()
    expect(within(row).getByLabelText('fct_order_lines × 注文ステータス')).toBeInTheDocument()
    expect(within(row).queryByLabelText('fct_order_lines × 種類')).not.toBeInTheDocument()
    // バスマトリクスがあるときは fact / dimension の一覧は出さない。マトリクスに無いモデルだけ一覧にする
    expect(screen.queryByRole('region', { name: 'dimension' })).not.toBeInTheDocument()
    expect(screen.getByText('dim_unregistered')).toBeInTheDocument()
    expect(screen.queryByText('dim_store', { selector: 'li span' })).not.toBeInTheDocument()
  })

  it('検索で fact と、結合できる dimension に絞り込む', () => {
    const two = {
      ...bus,
      rows: [...bus.rows, { name: 'fct_returns', label: '返品', groups: ['dim_promotion'] }],
    }
    render(<DatasetsPanel datasets={{ ...datasets, bus: two }} />)
    expect(screen.getByTestId('bus-fct_returns')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('使えるデータを検索'), { target: { value: '販売チャネル' } })
    expect(screen.getByTestId('bus-fct_order_lines')).toBeInTheDocument()
    expect(screen.queryByTestId('bus-fct_returns')).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: '種類' })).not.toBeInTheDocument()
  })

  it('✓ はその切り口のチャートへ、空欄は要求に入れる（#70）', () => {
    const onRequest = vi.fn()
    const groups = [
      ...bus.groups,
      { name: 'dim_customer', label: '顧客', kind: 'dim', attributes: [{ name: 'customer__region', label: '地方' }] },
      { name: 'fct_returns', label: '返品', kind: 'degenerate', attributes: [{ name: 'return__reason', label: '理由' }] },
    ]
    const rows = [
      { ...bus.rows[0], groups: [...bus.rows[0].groups, 'dim_customer'] },
      { name: 'fct_new', label: '新しい出来事', groups: [], candidate: true },
    ]
    const metrics = [
      { ...datasets.metrics[0], name: 'rate', label: '率', type: 'ratio', dimensions: [] },
      datasets.metrics[0],
    ]
    render(<DatasetsPanel datasets={{ ...datasets, metrics, bus: { groups, rows } }} onRequest={onRequest} />)
    const row = screen.getByTestId('bus-fct_order_lines')
    // その属性で切れる指標（単純な指標を優先）を、その切り口で開く
    expect(within(row).getByRole('link', { name: '売上金額を店舗の販売チャネル別にチャートで見る' })).toHaveAttribute(
      'href',
      '?chart=revenue&dimension=store__channel',
    )
    // 指標の切り口に無い属性は、切り口なしで開くと明示する
    const unsliced = within(row).getAllByRole('link', { name: '売上金額をチャートで見る（この切り口では切れません）' })
    expect(unsliced.length).toBe(2) // 注文ステータス・地方
    expect(unsliced.every((a) => a.getAttribute('href') === '?chart=revenue')).toBe(true)
    fireEvent.click(within(row).getByRole('button', { name: '要求に入れる: 「注文」をプロモーションの種類別に見たい' }))
    expect(onRequest).toHaveBeenCalledWith('「注文」をプロモーションの種類別に見たい')
    // 別の fact 自身の属性の空欄・候補の行は操作できない
    expect(within(row).queryByRole('button', { name: /理由別/ })).not.toBeInTheDocument()
    expect(within(screen.getByTestId('bus-fct_new')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('fact の列のまとまりの見出しは「縮退ディメンション」（#148）', () => {
    render(<DatasetsPanel datasets={{ ...datasets, bus }} />)
    const header = screen.getByRole('columnheader', { name: /縮退ディメンション/ })
    expect(header).toHaveAttribute('title', expect.stringContaining('日付で切るときは日付（dim_date）を使う'))
    expect(screen.queryByText('fact 内の属性')).not.toBeInTheDocument()
    render(<DatasetsPanel datasets={{ ...datasets, bus }} compact />)
    expect(screen.getAllByText('縮退ディメンション（注文）').length).toBeGreaterThan(0)
  })

  it('compact では dimension ごとに 1 列にする', () => {
    render(<DatasetsPanel datasets={{ ...datasets, bus }} compact />)
    const row = screen.getByTestId('bus-fct_order_lines')
    expect(within(row).getByLabelText('fct_order_lines × 店舗')).toBeInTheDocument()
    expect(within(row).queryByLabelText('fct_order_lines × プロモーション')).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: '販売チャネル' })).not.toBeInTheDocument()
  })
})

describe('exampleRequests', () => {
  it('日時や ID の列は切り口にしない', () => {
    expect(exampleRequests(datasets.sources)).toEqual(['返品理由別の返品の返品数量を見たい'])
  })
})

describe('exampleRequests の偏り', () => {
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

  it('テーブルを順に回して 1 件ずつ選ぶ', () => {
    const out = exampleRequests([t('raw.a', '注文'), t('raw.b', '返品')], 2)
    expect(out).toEqual(['チャネル別の注文の数量を見たい', 'チャネル別の返品の数量を見たい'])
  })

  it('日本語の名前が無い源泉・列は例にしない（英語の識別子を出さない。#148）', () => {
    const noTable = { ...t('raw.inventory', ''), description: '' }
    const noColumn = {
      ...t('raw.c', '在庫'),
      columns: [
        { name: 'quantity', description: '数量', data_type: 'INTEGER' },
        { name: 'movement_type', description: '', data_type: 'VARCHAR' },
      ],
    }
    expect(exampleRequests([noTable, noColumn])).toEqual([])
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

describe('similarMetrics', () => {
  // 実際のカタログと同じ切り口（販売チャネル・注文日を含む）
  const dims = ['店舗名', '販売チャネル', '商品カテゴリ', '商品名', '会員ランク', '注文日', '注文ステータス'].map((label, i) => ({
    name: `d${i}`,
    label,
    description: '',
    kind: 'categorical',
    entity: null,
  }))
  const metric = (name: string, label: string, description: string) => ({
    ...datasets.metrics[0],
    name,
    label,
    description,
    dimensions: dims,
  })
  const metrics = [
    metric('sales', '売上金額(税抜)', '店舗別の売上金額（キャンセルと返品を除く）の合計はいくらか'),
    metric('sales_by_product', '商品別売上金額', '商品別の売上金額'),
    metric('quantity', '商品別販売数量', '販売した数量（キャンセルと返品を除く）'),
    metric('daily', '日別売上金額', '日ごとの売上金額'),
  ]

  it('何を測るか（表示名の「◯◯別」の後ろ）が似た指標を出す（#75）', async () => {
    const { similarMetrics } = await import('@/api/datasets')
    const names = (r: string) => similarMetrics(r, metrics).map((m) => m.name)
    expect(names('店舗ごとの売上を見たい')).toEqual(expect.arrayContaining(['sales']))
    expect(names('売上を見たい')).toContain('sales')
    expect(names('販売数量を商品ごとに')).toEqual(['quantity'])
    expect(names('日別の売上')).toContain('daily')
  })

  it('切り口や汎用の言葉（数量・金額）だけが同じ別の指標は出さない（レビュー）', async () => {
    const { similarMetrics } = await import('@/api/datasets')
    for (const r of [
      '商品カテゴリ別の返品率を知りたい',
      '店舗ごと・商品カテゴリごとの入荷数量を月別に見たい',
      '在庫数量を見たい',
      '返品数量',
      'キャンセル数量',
      '返品金額',
      '値引金額',
      '会員ランク別の顧客数',
    ]) {
      expect([r, similarMetrics(r, metrics)]).toEqual([r, []])
    }
  })
})
