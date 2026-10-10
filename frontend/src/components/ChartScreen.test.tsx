import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '@/App'
import { chartUrl, svgDataUrl } from '@/api/charts'
import { JobProgress } from '@/components/JobProgress'

const OPTIONS = {
  preview: false,
  errors: [],
  metrics: [
    {
      name: 'revenue',
      label: '売上金額',
      description: '',
      type: 'simple',
      model: 'fct_order_lines',
      beam: null,
      dimensions: [
        { name: 'metric_time', label: '日付', description: '', kind: 'time', entity: null },
        { name: 'store__channel', label: 'チャネル', description: '', kind: 'categorical', entity: 'store' },
      ],
    },
  ],
}

function stubFetch(render: (body: Record<string, unknown>) => Response) {
  const calls: { url: string; body: Record<string, unknown> | null }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null
      calls.push({ url, body })
      if (url.includes('/charts/options')) return Promise.resolve(new Response(JSON.stringify(OPTIONS)))
      return Promise.resolve(render(body ?? {}))
    }),
  )
  return calls
}

describe('描画画面', () => {
  beforeEach(() => window.history.replaceState(null, '', chartUrl('revenue')))
  afterEach(() => {
    window.history.replaceState(null, '', '/')
    vi.unstubAllGlobals()
  })

  it('指標を開くと月別で描画し、切り口を変えると描き直す', async () => {
    const calls = stubFetch(
      (body) =>
        new Response(
          JSON.stringify({ svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>', kind: 'line', title: `売上金額 ${String(body.dimension)}`, sql: 'SELECT 1' }),
        ),
    )
    render(<App />)
    expect(await screen.findByAltText('売上金額 null')).toHaveAttribute('src', expect.stringContaining('data:image/svg+xml'))
    const first = calls.find((c) => c.url.includes('/charts/render'))
    expect(first?.body).toEqual({ metric: 'revenue', dimension: null, grain: 'month', start: null, end: null })

    // 時間の切り口は「切り口」の選択肢に出さない
    const select = screen.getByLabelText('切り口')
    expect(Array.from((select as HTMLSelectElement).options).map((o) => o.text)).toEqual(['なし', 'チャネル'])
    fireEvent.change(select, { target: { value: 'store__channel' } })
    expect(await screen.findByAltText('売上金額 store__channel')).toBeInTheDocument()
  })

  it('描画できない理由を表示する', async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: '期間の開始が終了より後になっています' }), { status: 422 }))
    render(<App />)
    expect(await screen.findByText('期間の開始が終了より後になっています')).toBeInTheDocument()
  })

  it('描き直しに失敗したら前のチャートを消す', async () => {
    let fail = false
    stubFetch(() =>
      fail
        ? new Response(JSON.stringify({ detail: '描画できませんでした' }), { status: 422 })
        : new Response(JSON.stringify({ svg: '<svg/>', kind: 'line', title: '月別', sql: '' })),
    )
    render(<App />)
    expect(await screen.findByAltText('月別')).toBeInTheDocument()
    fail = true
    fireEvent.change(screen.getByLabelText('粒度'), { target: { value: 'week' } })
    expect(await screen.findByText('描画できませんでした')).toBeInTheDocument()
    expect(screen.queryByAltText('月別')).toBeNull()
  })

  it('無い指標は理由だけを出す', async () => {
    window.history.replaceState(null, '', chartUrl('no_such'))
    stubFetch(() => new Response('{}'))
    render(<App />)
    expect(await screen.findByText(/指標 no_such がありません/)).toBeInTheDocument()
    expect(screen.queryByText('描画しています…')).toBeNull()
  })

  it('生成ジョブからはその PR の内容でプレビューする', async () => {
    window.history.replaceState(null, '', chartUrl('revenue', 'job1'))
    const calls = stubFetch(() => new Response(JSON.stringify({ svg: '<svg/>', kind: 'line', title: 't', sql: '' })))
    render(<App />)
    await waitFor(() => expect(calls.some((c) => c.url.includes('/charts/render?job_id=job1'))).toBe(true))
    expect(calls[0].url).toContain('/charts/options?job_id=job1')
  })
})

describe('完了画面からの遷移', () => {
  it('PR ができたら作った指標の描画画面へのリンクを出す', () => {
    render(
      <JobProgress
        job={{ id: 'job1', status: 'succeeded', steps: [], pr_url: 'https://github.com/o/r/pull/1', branch: 'beam/x' }}
        metrics={[{ name: 'revenue', label: '売上金額' }]}
      />,
    )
    expect(screen.getByRole('link', { name: /売上金額/ })).toHaveAttribute('href', '?chart=revenue&job=job1')
  })

  it('PR が無ければリンクを出さない', () => {
    render(<JobProgress job={{ id: 'j', status: 'failed', steps: [], error: 'x' }} metrics={[{ name: 'revenue', label: '売上金額' }]} />)
    expect(screen.queryByRole('link')).toBeNull()
  })
})

it('SVG は data URL にして <img> で表示する', () => {
  expect(svgDataUrl('<svg onload="x">')).toBe('data:image/svg+xml;charset=utf-8,%3Csvg%20onload%3D%22x%22%3E')
})
