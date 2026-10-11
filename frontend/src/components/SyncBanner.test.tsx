import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'

import { SyncBanner } from './SyncBanner'

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

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

const auto = (over = {}) => ({ enabled: true, interval_s: 60, last_checked_at: null, last_note: null, ...over })
const done = (id: string, st: 'succeeded' | 'failed') => ({ id, status: st, steps: [], error: st === 'failed' ? 'dbt build が失敗しました' : null })

describe('SyncBanner の自動反映（#106）', () => {
  it('問い合わせの間に始まって終わった反映も、結果を出して一覧を読み直す（1 回だけ）', async () => {
    window.localStorage.setItem('adp.sync.seenJob', 'old')
    const statusCall = vi
      .spyOn(api, 'syncStatus')
      .mockResolvedValueOnce({ status: status(), job: done('old', 'succeeded'), auto: auto() })
      .mockResolvedValue({ status: status({ behind: 0 }), job: done('new', 'succeeded'), auto: auto() })
    const events = vi.spyOn(api, 'jobEvents')
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} pollMs={20} />)
    expect(await screen.findByText('反映しました。')).toBeInTheDocument()
    const after = statusCall.mock.calls.length
    await waitFor(() => expect(statusCall.mock.calls.length).toBeGreaterThan(after + 3))
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(events).not.toHaveBeenCalled()
    expect(window.localStorage.getItem('adp.sync.seenJob')).toBe('new')
  })

  it('別の画面を開いている間に終わった反映は、開始画面に戻ったときに一覧を読み直す', async () => {
    window.localStorage.setItem('adp.sync.seenJob', 'before')
    vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status({ behind: 0 }), job: done('while-away', 'succeeded'), auto: auto() })
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} />)
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
  })

  it('自動反映の失敗は、取り込んだ後（新しい変更が 0 件）でも出す', async () => {
    vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status({ behind: 0, can_rebuild: true }), job: done('a', 'failed'), auto: auto() })
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} />)
    expect(await screen.findByText(/dbt build が失敗しました/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'もう一度実行する' })).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('このブラウザで初めて開いたときは、前からある成功の結果を出さない', async () => {
    const call = vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status({ behind: 0, can_rebuild: true }), job: done('a', 'succeeded'), auto: auto() })
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} />)
    await waitFor(() => expect(call).toHaveBeenCalled())
    expect(await screen.findByRole('button', { name: 'dbt を作り直して一覧を更新する' })).toBeInTheDocument()
    expect(screen.queryByText('反映しました。')).not.toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('前の反映の後も、新しい変更と自動で取り込まない理由を出す', async () => {
    window.localStorage.setItem('adp.sync.seenJob', 'x')
    vi.spyOn(api, 'syncStatus').mockResolvedValue({
      status: status({ behind: 1 }),
      job: done('prev', 'succeeded'),
      auto: auto({ last_note: 'インタビューの途中です。終わってから自動で取り込みます' }),
    })
    render(<SyncBanner />)
    expect(await screen.findByText('反映しました。')).toBeInTheDocument()
    expect(screen.getByText(/main に新しい変更があります（1 件/)).toBeInTheDocument()
    expect(screen.getByText(/インタビューの途中です/)).toBeInTheDocument()
  })

  it('反映を追っている間に状態の問い合わせが失敗しても、表示を消さない', async () => {
    let resolveEvents: () => void = () => {}
    vi.spyOn(api, 'syncStatus')
      .mockResolvedValueOnce({ status: status(), job: { id: 'r', status: 'running', steps: [] }, auto: auto() })
      .mockRejectedValue(new Error('network'))
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      await new Promise<void>((r) => (resolveEvents = r))
      yield { event: 'done' as const, data: { id: 'r', status: 'succeeded' as const, steps: [] } }
    })
    render(<SyncBanner pollMs={20} />)
    expect(await screen.findByTestId('sync-banner')).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.getByTestId('sync-banner')).toBeInTheDocument()
    resolveEvents()
  })

  it('取り込みを押した直後の問い合わせが同じジョブを返しても、二重に追わない', async () => {
    let resolveStart: (j: { id: string; status: 'running'; steps: never[] }) => void = () => {}
    const running = { id: 'j', status: 'running' as const, steps: [] }
    // 取り込みを押すまでは実行中のジョブは無い（押す前の問い合わせで追い始めないように）
    let started = false
    vi.spyOn(api, 'syncStatus').mockImplementation(async () => ({
      status: status(),
      job: started ? running : null,
      auto: auto(),
    }))
    vi.spyOn(api, 'startSync').mockImplementation(() => {
      started = true
      return new Promise((r) => (resolveStart = r))
    })
    const events = vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      yield { event: 'done' as const, data: { id: 'j', status: 'succeeded' as const, steps: [] } }
    })
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} pollMs={20} />)
    fireEvent.click(await screen.findByRole('button', { name: /取り込んで更新する/ }))
    await new Promise((r) => setTimeout(r, 100)) // 応答を待つ間に問い合わせが何度か返る
    resolveStart(running)
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 100))
    expect(events).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('見ていないタブでは問い合わせない', async () => {
    const vis = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const call = vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status(), job: null })
    render(<SyncBanner pollMs={20} />)
    await new Promise((r) => setTimeout(r, 100))
    expect(call).not.toHaveBeenCalled()
    vis.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await waitFor(() => expect(call).toHaveBeenCalled())
  })

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
    // 終わった後も同じジョブが返り続けるが、読み直しは 1 回だけ
    const after = statusCall.mock.calls.length
    await waitFor(() => expect(statusCall.mock.calls.length).toBeGreaterThan(after + 3))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('進捗のストリームが途中で切れたら、実行中のまま固めずにやり直せる', async () => {
    vi.spyOn(api, 'syncStatus').mockResolvedValue({ status: status(), job: null })
    vi.spyOn(api, 'startSync').mockResolvedValue({ id: 'j', status: 'running', steps: [] })
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {})
    const onDone = vi.fn()
    render(<SyncBanner onDone={onDone} />)
    fireEvent.click(await screen.findByRole('button', { name: /取り込んで更新する/ }))
    expect(await screen.findByRole('button', { name: 'もう一度実行する' })).toBeInTheDocument()
    expect(screen.getByText('進捗を受け取れなくなりました。もう一度実行してください')).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()
  })
})
