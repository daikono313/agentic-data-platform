import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import type { CleansingSpec, ColumnProfile, OnboardingDraft } from '@/api/datasets'

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

function column(name: string, data_type: string, extra: Partial<ColumnProfile> = {}): ColumnProfile {
  return {
    column: name,
    data_type,
    row_count: 30,
    null_count: 0,
    distinct_count: 30,
    min: null,
    max: null,
    top_values: [],
    is_unique: false,
    ...extra,
  }
}

/** フォームで直せる定義と、列のプロファイルのある下書き（#150） */
const formDraft: OnboardingDraft = {
  ...draft,
  patch: {
    ...draft.patch,
    concepts: [
      {
        ...draft.patch.concepts[0],
        label: '在庫の入出庫',
        attributes: [
          { name: 'inventory_id', label: '入出庫 ID', column: 'inventory_id' },
          { name: 'movement_type', label: '入出庫区分', column: 'movement_type' },
          { name: 'quantity', label: '数量', column: 'quantity' },
          { name: 'moved_at', label: '入出庫日時', column: 'moved_at' },
        ],
      },
    ],
  },
  cleansing_spec: {
    source: 'raw.inventory',
    key: 'inventory_id',
    columns: [
      { name: 'movement_type', accepted_values: ['receipt', 'shipment'] },
      { name: 'moved_at', cast: 'timestamp' },
    ],
  },
  columns: [
    column('inventory_id', 'BIGINT', { is_unique: true, min: '1', max: '30', histogram: [[1, 30, 30]] }),
    column('movement_type', 'VARCHAR', {
      distinct_count: 3,
      top_values: [
        ['receipt', 20],
        ['shipment', 9],
        ['adjustment', 1],
      ],
    }),
    column('quantity', 'INTEGER', { distinct_count: 20, min: '1', max: '50', histogram: [[1, 50, 30]] }),
    column('moved_at', 'VARCHAR'),
  ],
  concept_labels: { store: '店舗', inventory: '在庫の入出庫' },
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
    const [, patch, cleansing, redo] = start.mock.calls[0]
    expect(redo).toBe(false)
    expect(patch.concepts[0].label).toBe('在庫の入出庫')
    expect(patch.concepts[0].attributes[0].label).toBe('数量')
    expect(patch.relations[0].verb).toBe('入出庫する')
    expect(cleansing).toContain('key: inventory_id')
    expect(await screen.findByRole('link', { name: /作成した PR を開く/ })).toHaveAttribute(
      'href',
      'https://example.test/pr/1',
    )
  })

  it('実行中の取り込みの進捗が途中で切れたら、実行中のまま固めずに失敗を出す', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({
      job: { id: 'j1', status: 'running', steps: [{ step: 'worktree', label: '作業ブランチを作成', status: 'running', detail: '' }] },
    })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {})
    render(<OnboardingScreen table="raw.inventory" />)
    expect(await screen.findByText(/進捗の受信が途切れました/)).toBeInTheDocument()
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

  it('定義をフォームで読めないときは YAML で直し、エージェントの案に作り直しても残す', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({ job: null })
    vi.spyOn(api, 'onboardingDraft')
      .mockResolvedValueOnce(draft)
      .mockResolvedValueOnce({ ...draft, proposer: 'agent', cleansing: 'source: raw.inventory\n' })
    render(<OnboardingScreen table="raw.inventory" />)
    const yaml = await screen.findByLabelText('クレンジングの定義（YAML）')
    fireEvent.change(yaml, { target: { value: 'source: raw.inventory\nkey: edited\n' } })
    fireEvent.click(screen.getByRole('button', { name: /エージェントに案を作らせる/ }))
    expect(await screen.findByText('エージェントの案')).toBeInTheDocument()
    expect(screen.getByLabelText('クレンジングの定義（YAML）')).toHaveValue('source: raw.inventory\nkey: edited\n')
  })

  it('項目は日本語の見出しで出し、英語の名前は補足にする。属性の横に実際の値の例を出す', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({ job: null })
    vi.spyOn(api, 'onboardingDraft').mockResolvedValue(formDraft)
    render(<OnboardingScreen table="raw.inventory" />)
    const concept = await screen.findByTestId('concept-inventory')
    expect(within(concept).getByText('概念')).toBeInTheDocument()
    expect(within(concept).getByText('1 件を見分けるキー')).toBeInTheDocument()
    expect(screen.getByTestId('concept-key-inventory')).toHaveTextContent('入出庫 ID')
    // 英語の内部名は小さな補足（code）としてだけ出す
    expect(within(concept).getByText('inventory').tagName).toBe('CODE')
    const kind = screen.getByTestId('attribute-movement_type')
    expect(kind).toHaveTextContent('文字')
    expect(kind).toHaveTextContent('receipt')
    expect(screen.getByTestId('attribute-quantity')).toHaveTextContent('1 〜 50')
    // 関係は概念の表示名で出す
    const relation = screen.getByLabelText('関係 store → inventory の動詞').closest('li')
    expect(relation).toHaveTextContent('店舗→')
    expect(relation).toHaveTextContent('→在庫の入出庫')
  })

  it('クレンジングの定義を YAML を書かずにフォームで直し、直した内容で取り込みを始める', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({ job: null })
    vi.spyOn(api, 'onboardingDraft')
      .mockResolvedValueOnce(formDraft)
      .mockResolvedValueOnce({ ...formDraft, proposer: 'agent' })
    const start = vi.spyOn(api, 'startOnboarding').mockResolvedValue({ id: 'j1', status: 'running', steps: [] })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {})
    render(<OnboardingScreen table="raw.inventory" />)
    expect(await screen.findByLabelText('1 件を見分けるキー')).toHaveValue('inventory_id')
    expect(screen.queryByLabelText('クレンジングの定義（YAML）')).not.toBeInTheDocument()
    // 今のデータにあるが、許していない値（adjustment）も選べる
    expect(screen.getByLabelText('movement_type に許す値 receipt')).toBeChecked()
    fireEvent.click(screen.getByLabelText('movement_type に許す値 adjustment'))
    fireEvent.click(screen.getByLabelText('moved_at から日付の列を作る'))
    expect(screen.getByTestId('derive-moved_at')).toHaveTextContent('moved_date')
    // 直した定義は、エージェントの案に作り直しても残す
    fireEvent.click(screen.getByRole('button', { name: /エージェントに案を作らせる/ }))
    expect(await screen.findByText('エージェントの案')).toBeInTheDocument()
    expect(screen.getByLabelText('movement_type に許す値 adjustment')).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '確定して PR を作る' }))
    await waitFor(() => expect(start).toHaveBeenCalled())
    const spec = start.mock.calls[0][2] as CleansingSpec
    expect(spec.key).toBe('inventory_id')
    const rules = Object.fromEntries((spec.columns ?? []).map((r) => [r.name, r]))
    expect(rules.movement_type.accepted_values).toEqual(['receipt', 'shipment', 'adjustment'])
    expect(rules.moved_at.cast).toBe('timestamp')
    expect(rules.moved_at.derive).toEqual([{ name: 'moved_date', as: 'date', description: '入出庫日時の日付' }])
  })

  it('取り込み済みならその結果を出し、PR を閉じた場合はやり直せる', async () => {
    vi.spyOn(api, 'onboardingStatus').mockResolvedValue({
      job: { id: 'j0', status: 'succeeded', steps: [], pr_url: 'https://example.test/pr/0' },
    })
    const load = vi.spyOn(api, 'onboardingDraft')
    render(<OnboardingScreen table="raw.inventory" />)
    expect(await screen.findByRole('link', { name: /作成した PR を開く/ })).toBeInTheDocument()
    expect(load).not.toHaveBeenCalled()
    load.mockResolvedValue(draft)
    const start = vi.spyOn(api, 'startOnboarding').mockResolvedValue({ id: 'j2', status: 'running', steps: [] })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {})
    fireEvent.click(screen.getByRole('button', { name: 'PR を閉じたので、やり直す' }))
    fireEvent.click(await screen.findByRole('button', { name: '確定して PR を作る' }))
    await waitFor(() => expect(start).toHaveBeenCalled())
    expect(start.mock.calls[0][3]).toBe(true)
  })
})
