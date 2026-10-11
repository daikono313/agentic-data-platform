import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import demo from '@/demo/sales_by_store.json'

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

  it('?metrics で指標の一覧のページを開き、q を検索語の初期値にする（#107）', async () => {
    window.history.replaceState(null, '', '/?metrics&q=%E5%A3%B2%E4%B8%8A')
    render(<App />)
    expect(screen.getByRole('heading', { name: '指標の一覧' })).toBeInTheDocument()
    expect(await screen.findByText('まだありません。要求すると作れます。')).toBeInTheDocument()
    expect(screen.getByLabelText('指標を検索')).toHaveValue('売上')
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

describe('インタビュー画面（確定ボタンを押せない理由。#108）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  const issue = '設計: 指標 sales の group_by が見つかりません'
  const datasets = { metrics: [], models: [], beams: [], errors: [], sources: [] }
  const reviewSession = (issues: string[]) => ({
    id: 's1',
    phase: 'review',
    beam: demo.beam,
    matrix: demo.matrix,
    issues,
    choices: [],
    question: null,
    messages: [],
    job: null,
    versions: [],
    feedback: [],
  })

  /** backend の代わり。GET /sessions/s1 は current を返し、ほかは handlers で応じる */
  function stubBackend(current: { session: ReturnType<typeof reviewSession> }, handlers: Record<string, () => Response>) {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      for (const [suffix, handler] of Object.entries(handlers)) {
        if (url.endsWith(suffix) && init?.method === 'POST') return Promise.resolve(handler())
      }
      if (url.includes('/datasets')) return Promise.resolve(new Response(JSON.stringify(datasets)))
      if (url.includes('/feedback')) return Promise.resolve(new Response('{}'))
      return Promise.resolve(new Response(JSON.stringify(current.session)))
    })
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/?session=s1')
    return fetchMock
  }

  it('確定までの課題が残った review の画面で、ボタンの近くに理由と課題を出す', async () => {
    stubBackend({ session: reviewSession([issue]) }, {})
    render(<App />)
    const button = await screen.findByRole('button', { name: '確定して実装へ' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription(/確定までに残っている課題が 1 件あります/)
    expect(screen.getByRole('list', { name: '確定までに残っている課題' })).toHaveTextContent(issue)
  })

  it('会話の beam イベントで課題が無くなると、ボタンが押せるようになる', async () => {
    const current = { session: reviewSession([issue]) }
    const sse = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
    stubBackend(current, {
      '/messages': () => {
        current.session = reviewSession([])
        return new Response(
          sse('beam', { beam: demo.beam, matrix: demo.matrix, issues: [] }) +
            sse('done', { message: { role: 'assistant', text: '直しました' } }),
        )
      },
    })
    render(<App />)
    const button = await screen.findByRole('button', { name: '確定して実装へ' })
    expect(button).toBeDisabled()
    fireEvent.change(screen.getByLabelText('回答を入力'), { target: { value: '店舗別で見たい' } })
    fireEvent.click(screen.getByRole('button', { name: '送信' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeEnabled())
    expect(screen.queryByRole('list', { name: '確定までに残っている課題' })).not.toBeInTheDocument()
  })

  it('確定が 409 で断られたら、読める理由をボタンの近くに出し、セッションを読み直す', async () => {
    const current = { session: reviewSession([]) }
    const fetchMock = stubBackend(current, {
      '/finalize': () => {
        // 別のタブで課題が増えていた、など。読み直すと課題の一覧が新しくなる
        current.session = reviewSession([issue])
        return new Response(JSON.stringify({ detail: `確定できません: ${issue}` }), { status: 409 })
      },
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '確定して実装へ' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(`確定できませんでした: 確定できません: ${issue}`)
    expect(screen.getByRole('alert')).not.toHaveTextContent('detail')
    expect(await screen.findByRole('list', { name: '確定までに残っている課題' })).toHaveTextContent(issue)
    expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeDisabled()
    const gets = fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith('/sessions/s1') && !init?.method)
    expect(gets.length).toBeGreaterThanOrEqual(2)
  })

  it('main の反映中・別のタブで応答中などほかの 409 も、同じくボタンの近くに出す', async () => {
    stubBackend(
      { session: reviewSession([]) },
      {
        '/finalize': () =>
          new Response(JSON.stringify({ detail: 'main の反映（dbt build）を実行中です。終わってから実行してください' }), {
            status: 409,
          }),
      },
    )
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '確定して実装へ' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('main の反映（dbt build）を実行中です')
    expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeEnabled()
  })

  it('確定済みでジョブが無いときは、確定し直す道を出す', async () => {
    stubBackend({ session: { ...reviewSession([]), phase: 'finalized' } }, {})
    render(<App />)
    expect(await screen.findByRole('button', { name: '修正して確定し直す' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '確定して実装へ' })).not.toBeInTheDocument()
  })
})
