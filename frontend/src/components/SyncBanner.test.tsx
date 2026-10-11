import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'

import { SyncBanner } from './SyncBanner'

afterEach(() => vi.restoreAllMocks())

const status = (over = {}) => ({ branch: 'main', clean: true, behind: 2, can_pull: true, reason: null, ...over })

describe('SyncBanner', () => {
  it('main に新しい変更があれば知らせ、取り込んで更新できる（#74）', async () => {
    vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status(), job: null })
    const start = vi.spyOn(api, 'startSync').mockResolvedValue({ id: 'j', status: 'running', steps: [] })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      yield { event: 'done' as const, data: { id: 'j', status: 'succeeded' as const, steps: [] } }
    })
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} />)
    expect(await screen.findByText(/main に新しい変更があります（2 件/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /取り込んで更新する/ }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(start).toHaveBeenCalled()
    expect(screen.getByText('反映しました。')).toBeInTheDocument()
  })

  it('取り込めないときは理由を出し、ボタンを出さない', async () => {
    vi.spyOn(api, 'syncStatus').mockResolvedValue({
      status: status({ branch: 'feat/x', can_pull: false, can_rebuild: false, reason: '手元が main ではありません（feat/x）' }),
      job: null,
    })
    render(<SyncBanner />)
    expect(await screen.findByText(/手元が main ではありません/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('新しい変更が無ければ、dbt の作り直しだけを小さく出す（手で取り込んだ後など）', async () => {
    const call = vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status({ behind: 0, can_rebuild: true }), job: null })
    render(<SyncBanner />)
    await waitFor(() => expect(call).toHaveBeenCalled())
    expect(screen.queryByTestId('sync-banner')).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'dbt を作り直して一覧を更新する' })).toBeInTheDocument()
  })

  it('失敗したら、もう一度実行できる', async () => {
    vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status(), job: null })
    vi.spyOn(api, 'startSync').mockResolvedValue({ id: 'j', status: 'running', steps: [] })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      yield { event: 'done' as const, data: { id: 'j', status: 'failed' as const, steps: [], error: 'x' } }
    })
    render(<SyncBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /取り込んで更新する/ }))
    expect(await screen.findByRole('button', { name: 'もう一度実行する' })).toBeInTheDocument()
  })
})

describe('SyncBanner の自動反映（#106）', () => {
  it('backend が自動で始めた反映に気づいて進捗を追い、終われば一覧を読み直す', async () => {
    const running = { id: 'auto', status: 'running' as const, steps: [] }
    const statusCall = vi
      .spyOn(api, 'syncStatus')
      .mockResolvedValueOnce({ status: status(), job: null, auto: { enabled: true, interval_s: 60, last_checked_at: null, last_note: null } })
      .mockResolvedValue({ status: status(), job: running, auto: { enabled: true, interval_s: 60, last_checked_at: null, last_note: null } })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      yield { event: 'done' as const, data: { id: 'auto', status: 'succeeded' as const, steps: [] } }
    })
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} pollMs={20} />)
    expect(await screen.findByText('まもなく自動で取り込みます')).toBeInTheDocument()
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(statusCall.mock.calls.length).toBeGreaterThan(1)
  })

  it('進捗のストリームが途中で切れたら、実行中のまま固めずにやり直せる', async () => {
    vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status(), job: null })
    vi.spyOn(api, 'startSync').mockResolvedValue({ id: 'j', status: 'running', steps: [] })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {})
    render(<SyncBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /取り込んで更新する/ }))
    expect(await screen.findByRole('button', { name: 'もう一度実行する' })).toBeInTheDocument()
  })
})
