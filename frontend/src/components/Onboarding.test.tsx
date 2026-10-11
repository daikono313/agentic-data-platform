import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import type { OnboardingDraft } from '@/api/datasets'

import { OnboardingScreen } from './Onboarding'

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}))

const draft: OnboardingDraft = {
  source: 'raw.inventory',
  proposer: 'profile',
  note: null,
  patch: {
    domains: [],
    concepts: [
      {
        name: 'inventory',
        label: 'inventory',
        domain: 'channel',
        kind: 'event',
        source: 'raw.inventory',
        key: 'inventory_id',
        attributes: [{ name: 'quantity', label: 'quantity', column: 'quantity' }],
      },
    ],
    relations: [{ from: 'store', to: 'inventory', verb: '関わる', via: 'raw.inventory.store_id' }],
    constraints: [],
  },
  cleansing: 'source: raw.inventory\nkey: inventory_id\n',
  cleansing_exists: false,
  mermaid: 'flowchart LR',
  bus: {
    groups: [{ name: 'dim_store', label: '店舗', kind: 'dim', attributes: [{ name: 'store__x', label: 'X' }] }],
    rows: [{ name: 'fct_inventory', label: '在庫', groups: ['dim_store'], candidate: true }],
  },
}

afterEach(() => vi.restoreAllMocks())

describe('OnboardingScreen', () => {
  it('追加案を直して確定すると、直した内容で取り込みを始める', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({ job: null })
    vi.spyOn(api, 'onboardingDraft').mockResolvedValue(draft)
    const start = vi.spyOn(api, 'startOnboarding').mockResolvedValue({
      id: 'j1',
      status: 'running',
      steps: [{ step: 'worktree', label: '作業ブランチを作成', status: 'running', detail: '' }],
    })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      yield {
        event: 'done' as const,
        data: { id: 'j1', status: 'succeeded' as const, steps: [], pr_url: 'https://example.test/pr/1' },
      }
    })
    render(<OnboardingScreen table="raw.inventory" />)
    const name = await screen.findByLabelText('概念の名前（表示名）')
    expect(api.onboardingDraft).toHaveBeenCalledWith('raw.inventory', false)
    expect(screen.getByTestId('bus-fct_inventory')).toHaveTextContent('（候補）')
    fireEvent.change(name, { target: { value: '在庫の入出庫' } })
    fireEvent.change(screen.getByLabelText('属性 quantity の表示名'), { target: { value: '数量' } })
    fireEvent.change(screen.getByLabelText('関係 store → inventory の動詞'), { target: { value: '入出庫する' } })
    fireEvent.click(screen.getByRole('button', { name: '確定して PR を作る' }))
    await waitFor(() => expect(start).toHaveBeenCalled())
    const [, patch, cleansing] = start.mock.calls[0]
    expect(patch.concepts[0].label).toBe('在庫の入出庫')
    expect(patch.concepts[0].attributes[0].label).toBe('数量')
    expect(patch.relations[0].verb).toBe('入出庫する')
    expect(cleansing).toContain('key: inventory_id')
    expect(await screen.findByRole('link', { name: /作成した PR を開く/ })).toHaveAttribute(
      'href',
      'https://example.test/pr/1',
    )
  })

  it('エージェントに案を作らせ直せる', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({ job: null })
    const load = vi
      .spyOn(api, 'onboardingDraft')
      .mockResolvedValueOnce(draft)
      .mockResolvedValueOnce({ ...draft, proposer: 'agent' })
    render(<OnboardingScreen table="raw.inventory" />)
    fireEvent.click(await screen.findByRole('button', { name: /エージェントに案を作らせる/ }))
    expect(await screen.findByText('エージェントの案')).toBeInTheDocument()
    expect(load).toHaveBeenLastCalledWith('raw.inventory', true)
  })

  it('取り込み済みならその結果を出す', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({
      job: { id: 'j0', status: 'succeeded', steps: [], pr_url: 'https://example.test/pr/0' },
    })
    const load = vi.spyOn(api, 'onboardingDraft')
    render(<OnboardingScreen table="raw.inventory" />)
    expect(await screen.findByRole('link', { name: /作成した PR を開く/ })).toBeInTheDocument()
    expect(load).not.toHaveBeenCalled()
  })
})
