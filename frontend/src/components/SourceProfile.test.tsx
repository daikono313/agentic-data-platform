import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import type { SourceProfile } from '@/api/datasets'

import { SourceProfileScreen } from './SourceProfile'

const col = (column: string, extra: Partial<SourceProfile['profiles'][number]>) => ({
  column,
  data_type: 'VARCHAR',
  row_count: 4,
  null_count: 0,
  distinct_count: 4,
  min: 'a',
  max: 'd',
  top_values: [] as [string | null, number][],
  is_unique: true,
  ...extra,
})

const profile: SourceProfile = {
  schema_name: 'raw',
  name: 'returns',
  description: '返品',
  row_count: 4,
  columns: [
    { name: 'return_id', description: '返品ID', data_type: 'INTEGER' },
    { name: 'order_id', description: '注文ID', data_type: 'INTEGER' },
    { name: 'return_reason', description: '返品理由', data_type: 'VARCHAR' },
  ],
  primary_key_candidates: ['return_id'],
  foreign_key_candidates: [{ column: 'order_id', references: 'raw.orders.order_id', match_ratio: 1 }],
  profiles: [
    col('return_id', { data_type: 'INTEGER', min: '1', max: '4' }),
    col('order_id', { data_type: 'INTEGER', distinct_count: 3, is_unique: false, top_values: [['10', 2]] }),
    col('return_reason', {
      null_count: 1,
      distinct_count: 2,
      is_unique: false,
      top_values: [
        ['サイズ違い', 2],
        [null, 1],
      ],
    }),
  ],
  used_by: ['fct_returns'],
}

beforeEach(() => {
  vi.spyOn(api, 'sourceCleansing').mockResolvedValue({ path: 'warehouse/cleansing/x.yml', existing: null, draft: 'source: x\n' })
})
afterEach(() => vi.restoreAllMocks())

describe('SourceProfileScreen', () => {
  it('列ごとの統計・キーの候補・使っているモデルを表示する', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue(profile)
    vi.spyOn(api, 'sourceCleansing').mockResolvedValue({
      path: 'warehouse/cleansing/raw.returns.yml',
      existing: null,
      draft: 'source: raw.returns\nkey: return_id\n',
    })
    render(<SourceProfileScreen table="raw.returns" />)
    expect(await screen.findByTestId('cleansing-yaml')).toHaveTextContent('key: return_id')
    expect(screen.getByText(/まだ定義がありません/)).toBeInTheDocument()
    const reason = await screen.findByTestId('column-return_reason')
    expect(api.sourceProfile).toHaveBeenCalledWith('raw.returns')
    expect(screen.getByRole('heading', { name: '返品' })).toBeInTheDocument()
    expect(reason).toHaveTextContent('25.0%') // NULL 率
    expect(reason).toHaveTextContent('サイズ違い')
    expect(within(reason).getByText('NULL')).toBeInTheDocument()
    expect(within(screen.getByTestId('column-return_id')).getByLabelText('主キー候補')).toBeInTheDocument()
    expect(screen.getByTestId('column-return_id')).toHaveTextContent('すべて異なる値')
    expect(screen.getByTestId('column-order_id')).toHaveTextContent('→ raw.orders.order_id')
    expect(screen.getByText('fct_returns')).toBeInTheDocument()
  })

  it('定義があれば今の定義を出し、雛形に切り替えられる', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue(profile)
    vi.spyOn(api, 'sourceCleansing').mockResolvedValue({
      path: 'warehouse/cleansing/raw.returns.yml',
      existing: 'source: raw.returns\ndescription: 今の定義\n',
      draft: 'source: raw.returns\ndescription: 雛形\n',
    })
    render(<SourceProfileScreen table="raw.returns" />)
    expect(await screen.findByTestId('cleansing-yaml')).toHaveTextContent('今の定義')
    fireEvent.click(screen.getByRole('button', { name: 'プロファイルからの雛形を見る' }))
    expect(screen.getByTestId('cleansing-yaml')).toHaveTextContent('雛形')
  })

  it('NULL だけの列・NULL 以外がすべて違う列・ほぼすべて違う列では上位の値を出さない', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue({
      ...profile,
      profiles: [
        col('note', { null_count: 4, distinct_count: 0, is_unique: false, min: null, max: null, top_values: [[null, 4]] }),
        col('returned_at', {
          row_count: 1000,
          distinct_count: 990,
          is_unique: false,
          top_values: [['2024-03-14 21:19:00', 2]],
        }),
        col('email', {
          null_count: 1,
          distinct_count: 3,
          is_unique: false,
          top_values: [
            [null, 1],
            ['a@example.com', 1],
          ],
        }),
      ],
    })
    render(<SourceProfileScreen table="raw.returns" />)
    expect(await screen.findByTestId('column-note')).toHaveTextContent('すべて NULL')
    expect(screen.getByTestId('column-email')).toHaveTextContent('すべて異なる値')
    expect(screen.queryByText('a@example.com')).not.toBeInTheDocument()
    expect(screen.getByTestId('column-returned_at')).toHaveTextContent('ほとんど異なる値')
  })

  it('月ごとの件数（欠けた月）・数値の分布・最新の日時・作れそうな指標を出す', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue({
      ...profile,
      columns: [
        { name: 'returned_at', description: '返品日時', data_type: 'TIMESTAMP' },
        { name: 'quantity', description: '返品数量', data_type: 'INTEGER' },
        { name: 'return_reason', description: '返品理由', data_type: 'VARCHAR' },
        { name: 'updated_at', description: '更新日時', data_type: 'TIMESTAMP' },
      ],
      profiles: [
        col('returned_at', {
          data_type: 'TIMESTAMP',
          is_unique: false,
          max: '2025-03-10 10:00:00',
          monthly: [
            ['2025-01', 3],
            ['2025-02', 0],
            ['2025-03', 1],
          ],
        }),
        col('quantity', {
          data_type: 'INTEGER',
          is_unique: false,
          distinct_count: 2,
          histogram: [
            [1, 2, 3],
            [2, 3, 1],
          ],
        }),
        col('return_reason', { is_unique: false, distinct_count: 2 }),
        col('updated_at', { data_type: 'TIMESTAMP', max: '2026-01-01 00:00:00', monthly: [['2026-01', 4], ['2026-02', 1]] }),
      ],
    })
    render(<SourceProfileScreen table="raw.returns" />)
    const monthly = await screen.findByTestId('monthly-returned_at')
    expect(monthly).toHaveTextContent('欠け 1 か月')
    expect(within(monthly).getByRole('img')).toHaveAttribute('aria-label', expect.stringContaining('欠け 1 か月'))
    expect(within(monthly).getByTitle('2025-02: 0 行')).toBeInTheDocument()
    expect(screen.getByTestId('histogram-quantity')).toBeInTheDocument()
    // 管理用の更新日時ではなく、業務の日時の最新を出す
    expect(screen.getByTestId('freshness')).toHaveTextContent('returned_at')
    expect(screen.getByTestId('freshness')).toHaveTextContent('2025-03-10 10:00:00')
    const link = screen.getByRole('link', { name: '返品理由別の返品の返品数量を見たい' })
    expect(link).toHaveAttribute('href', '/?request=' + encodeURIComponent('返品理由別の返品の返品数量を見たい').replace(/%20/g, '+'))
  })

  it('鮮度は型で日時の列を選び、月別が省略された長い期間の列も、未来の日時は除く', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue({
      ...profile,
      profiles: [
        col('ordered_at', { data_type: 'TIMESTAMP', is_unique: false, max: '2025-06-01 00:00:00', monthly: null }),
        col('expires_at', { data_type: 'TIMESTAMP', is_unique: false, max: '2999-01-01 00:00:00', monthly: null }),
      ],
    })
    render(<SourceProfileScreen table="raw.returns" />)
    const fresh = await screen.findByTestId('freshness')
    expect(fresh).toHaveTextContent('ordered_at')
    expect(fresh).toHaveTextContent('2025-06-01 00:00:00')
    expect(screen.getByTestId('column-ordered_at')).toHaveTextContent('期間が長いため月別は省略')
  })

  it('個人情報の列は値を出さない（#81）', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue({
      ...profile,
      profiles: [col('customer_name', { masked: true, min: null, max: null, top_values: [], is_unique: false })],
    })
    render(<SourceProfileScreen table="raw.customers" />)
    expect(await screen.findByTestId('column-customer_name')).toHaveTextContent('個人情報のため、値は表示しません')
  })

  it('取得に失敗したらエラーを表示する', async () => {
    vi.spyOn(api, 'sourceProfile').mockRejectedValue(new Error('{"detail":"対象外のテーブルです: x.y"}'))
    render(<SourceProfileScreen table="x.y" />)
    expect(await screen.findByText(/対象外のテーブルです/)).toBeInTheDocument()
  })
})
