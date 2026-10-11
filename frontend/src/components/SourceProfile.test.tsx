import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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

afterEach(() => vi.restoreAllMocks())

describe('SourceProfileScreen', () => {
  it('列ごとの統計・キーの候補・使っているモデルを表示する', async () => {
    vi.spyOn(api, 'sourceProfile').mockResolvedValue(profile)
    render(<SourceProfileScreen table="raw.returns" />)
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

  it('取得に失敗したらエラーを表示する', async () => {
    vi.spyOn(api, 'sourceProfile').mockRejectedValue(new Error('{"detail":"対象外のテーブルです: x.y"}'))
    render(<SourceProfileScreen table="x.y" />)
    expect(await screen.findByText(/対象外のテーブルです/)).toBeInTheDocument()
  })
})
