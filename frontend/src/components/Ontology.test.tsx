import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import type { OntologyView } from '@/api/datasets'

import { OntologyScreen } from './Ontology'

// jsdom では mermaid の描画（SVG の寸法の計算）ができないので、描画結果を差し替える
vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, text: string) => ({ svg: `<svg data-source="${text.split('\n')[0]}"></svg>` })),
  },
}))

const view: OntologyView = {
  mermaid: 'flowchart LR\n  c_customer("顧客")',
  ontology: {
    domains: [{ name: 'sales', label: '販売' }],
    concepts: [
      {
        name: 'customer',
        label: '顧客',
        domain: 'sales',
        kind: 'entity',
        source: 'raw.customers',
        key: 'customer_id',
        model: 'dim_customer',
        attributes: [{ name: 'region', label: '地方', column: 'region', synonyms: ['エリア'] }],
      },
      { name: 'order', label: '注文', domain: 'sales', kind: 'event', source: 'raw.orders', key: 'order_id', attributes: [] },
    ],
    relations: [{ from: 'customer', to: 'order', verb: '注文する', via: 'raw.orders.customer_id' }],
    constraints: [{ concept: 'order', text: 'キャンセルを含む' }],
  },
  unmapped_sources: ['raw.inventory'],
  unmapped_models: ['dim_date'],
  errors: ['概念 order: キー raw.orders.order_id が無い'],
  multiplicities: ['顧客 1 : 注文 多、注文には必ず顧客がある'],
}

afterEach(() => vi.restoreAllMocks())

describe('OntologyScreen', () => {
  it('図・概念・制約と、源泉との食い違いを表示する', async () => {
    vi.spyOn(api, 'ontology').mockResolvedValue(view)
    render(<OntologyScreen />)
    const diagram = await screen.findByTestId('mermaid')
    expect(diagram.querySelector('svg')).toHaveAttribute('data-source', 'flowchart LR')
    expect(screen.getByTestId('concept-customer')).toHaveTextContent('dim_customer')
    expect(screen.getByTestId('concept-order')).toHaveTextContent('未整備')
    expect(screen.getByText(/キャンセルを含む/)).toBeInTheDocument()
    expect(screen.getByRole('table', { name: '関係' })).toHaveTextContent(
      '顧客注文する注文raw.orders.customer_id顧客 1 : 注文 多、注文には必ず顧客がある',
    )
    expect(screen.getByTestId('concept-customer')).toHaveTextContent('地方（同義語: エリア）')
    expect(screen.getByText(/dim_date/)).toBeInTheDocument()
    expect(screen.getByText(/キー raw.orders.order_id が無い/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'raw.inventory（取り込む）' })).toHaveAttribute('href', '?onboard=raw.inventory')
    expect(screen.getByRole('link', { name: /ホーム/ })).toHaveAttribute('href', '/')
  })

  it('読み込めなければエラーを表示する', async () => {
    vi.spyOn(api, 'ontology').mockRejectedValue(new Error('{"detail":"ontology/ontology.yml がありません"}'))
    render(<OntologyScreen />)
    expect(await screen.findByText(/ontology.yml がありません/)).toBeInTheDocument()
  })
})
