import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'

import { HistoryScreen } from './History'

afterEach(() => vi.restoreAllMocks())

describe('HistoryScreen', () => {
  it('要求・状態・PR とその状態・作った指標を一覧にし、セッションを開ける', async () => {
    vi.spyOn(api, 'history').mockResolvedValue({
      sessions: [
        {
          id: 's1',
          title: '日別売上',
          request: '日ごとの売上を見たい',
          phase: 'finalized',
          updated_at: '2026-10-11T00:00:00',
          metrics: ['売上金額'],
          job: { id: 'j1', status: 'succeeded', steps: [], pr_url: 'https://github.com/o/r/pull/56' },
          pr_state: 'MERGED',
        },
        {
          id: 's2',
          title: '',
          request: '返品率を見たい',
          phase: 'grain',
          updated_at: '2026-10-11T01:00:00',
          metrics: [],
          job: null,
          pr_state: null,
        },
      ],
    })
    render(<HistoryScreen />)
    const first = await screen.findByTestId('session-s1')
    expect(first).toHaveTextContent('PR 作成済み')
    expect(first).toHaveTextContent('マージ済み')
    expect(first).toHaveTextContent('売上金額')
    expect(screen.getByRole('link', { name: '日別売上' })).toHaveAttribute('href', '?session=s1')
    expect(screen.getByRole('link', { name: /o\/r\/pull\/56/ })).toHaveAttribute('href', 'https://github.com/o/r/pull/56')
    expect(screen.getByTestId('session-s2')).toHaveTextContent('返品率を見たい')
  })

  it('まだ要求が無ければそう出す', async () => {
    vi.spyOn(api, 'history').mockResolvedValue({ sessions: [] })
    render(<HistoryScreen />)
    expect(await screen.findByText('まだ要求がありません。')).toBeInTheDocument()
  })
})
