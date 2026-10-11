import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ metrics: [], models: [], sources: [], beams: [], errors: [] })))))
  })
  afterEach(() => window.history.replaceState(null, '', '/'))

  it('最初は要求の入力画面を表示する', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: 'どんな指標を見たいですか？' })).toBeInTheDocument()
    expect(screen.getByLabelText('見たい指標')).toBeInTheDocument()
  })

  it('デモでは BEAM Canvas と Event Matrix を表示する', () => {
    window.history.replaceState(null, '', '/?demo')
    render(<App />)
    expect(screen.getByText('顧客が商品を注文する', { selector: 'h3' })).toBeInTheDocument()
    expect(screen.getByTestId('w-who')).toHaveTextContent('会員ランク')
    expect(screen.getByTestId('w-who')).toHaveTextContent('年収（データなし）')
    expect(screen.getByTestId('how-many')).toHaveTextContent('売上金額')
    expect(screen.getByLabelText('顧客が商品を注文する × 店舗')).toHaveTextContent('✓')
    expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeDisabled()
  })

  it('インタビュー画面からホーム（開始画面）に戻れる', () => {
    window.history.replaceState(null, '', '/?demo')
    render(<App />)
    expect(screen.getByRole('link', { name: 'ホーム' })).toHaveAttribute('href', '/')
  })
})

describe('App の開始画面（似た指標）', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('要求を入力すると、似た既にある指標のチャートへのリンクを出す（#75）', async () => {
    const body = {
      metrics: [
        {
          name: 'sales',
          label: '売上金額',
          description: '店舗別の売上金額',
          type: 'simple',
          model: 'fct_order_lines',
          beam: null,
          dimensions: [],
        },
      ],
      models: [],
      beams: [],
      errors: [],
      sources: [],
    }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(body)))))
    render(<App />)
    const input = screen.getByLabelText('見たい指標')
    fireEvent.change(input, { target: { value: '店舗ごとの売上を見たい' } })
    expect(await screen.findByRole('link', { name: '売上金額をチャートで見る' })).toHaveAttribute('href', '?chart=sales')
  })
})

describe('インタビュー画面（似た指標）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  it('似た既にある指標を参考に出す（確定は止めない。この BEAM 自身の指標は除く）', async () => {
    const metric = (name: string, label: string, beam: string | null) => ({
      name,
      label,
      description: '',
      type: 'simple',
      model: 'fct_order_lines',
      beam,
      dimensions: [],
    })
    const datasets = {
      metrics: [metric('sales', '売上金額', 'store_sales'), metric('mine', '返品率', 'returns_rate')],

      models: [],
      beams: [],
      errors: [],
      sources: [],
    }
    const session = {
      id: 's1',
      phase: 'review',
      beam: { schema_version: 1, name: 'returns_rate', title: '返品率', request: '売上と返品率を見たい', events: [], metrics: [], gaps: [] },
      matrix: { columns: [], rows: [] },
      issues: [],
      choices: [],
      question: null,
      messages: [],
      job: null,
      versions: [],
      feedback: [],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve(new Response(JSON.stringify(url.includes('/datasets') ? datasets : url.includes('/feedback') ? {} : session))),
      ),
    )
    window.history.replaceState(null, '', '/?session=s1')
    render(<App />)
    const box = await screen.findByRole('status', { name: '似ている既にある指標' })
    expect(box).toHaveTextContent('売上金額をチャートで見る')
    // この BEAM 自身の指標（返品率）は「似ている」には出さず、同じ名前で置き換えることを知らせる
    expect(box).not.toHaveTextContent('返品率をチャートで見る')
    expect(box).toHaveTextContent('同じ名前（returns_rate）の要求が既にあります（返品率）')
  })
})

describe('App の開始画面', () => {
  it('要求の例をクリックすると入力欄に入る', async () => {
    const body = {
      metrics: [],
      models: [],
      beams: [],
      errors: [],
      sources: [
        {
          name: 'raw.returns',
          description: '返品',
          row_count: 1,
          columns: [
            { name: 'quantity', description: '返品数量', data_type: 'INTEGER' },
            { name: 'return_reason', description: '返品理由', data_type: 'VARCHAR' },
          ],
        },
      ],
    }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(body)))))
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '返品理由別の返品の返品数量を見たい' }))
    expect(screen.getByLabelText('見たい指標')).toHaveValue('返品理由別の返品の返品数量を見たい')
  })
})
