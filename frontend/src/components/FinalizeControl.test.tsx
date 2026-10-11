import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { FinalizeControl } from './FinalizeControl'
import { finalizeBlockers } from './finalizeBlockers'

const noop = () => Promise.resolve()

describe('finalizeBlockers（#108）', () => {
  it('課題が無く、出来事があり、応答中でなければ押せる', () => {
    expect(finalizeBlockers({ issues: [], eventCount: 1, busy: false }).reason).toBeNull()
  })

  it('押せないときは、必ず理由を返す', () => {
    expect(finalizeBlockers({ issues: ['a'], eventCount: 1, busy: false }).reason).toContain('残っている課題が 1 件')
    expect(finalizeBlockers({ issues: [], eventCount: 0, busy: false }).reason).toContain('出来事')
    expect(finalizeBlockers({ issues: [], eventCount: 1, busy: true }).reason).toContain('応答の途中')
    expect(finalizeBlockers({ issues: [], eventCount: 1, busy: false, submitting: true }).reason).toContain('確定しています')
    expect(finalizeBlockers({ issues: [], eventCount: 1, busy: false, demo: true }).reason).toContain('デモ')
    expect(finalizeBlockers({ issues: [], eventCount: 1, busy: false, finalized: true }).reason).toContain('確定済み')
  })
})

describe('FinalizeControl（#108）', () => {
  it('課題が残っていると、ボタンを無効にし、理由と課題の一覧をボタンの近くに出す', () => {
    render(
      <FinalizeControl
        issues={['How many（measure）がありません', 'grain が決まっていません']}
        eventCount={1}
        busy={false}
        onFinalize={noop}
      />,
    )
    const button = screen.getByRole('button', { name: '確定して実装へ' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription(/残っている課題が 2 件/)
    const list = screen.getByRole('list', { name: '確定までに残っている課題' })
    expect(list).toHaveTextContent('How many（measure）がありません')
    expect(list).toHaveTextContent('grain が決まっていません')
  })

  it('課題が多いときは 8 件まで出し、残りの件数を出す', () => {
    const issues = Array.from({ length: 10 }, (_, i) => `課題 ${i + 1}`)
    render(<FinalizeControl issues={issues} eventCount={1} busy={false} onFinalize={noop} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(8)
    expect(screen.getByText('ほか 2 件')).toBeInTheDocument()
  })

  it('応答の途中は、課題が無くても理由を出す', () => {
    render(<FinalizeControl issues={[]} eventCount={1} busy onFinalize={noop} />)
    expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('応答の途中です')
  })

  it('出来事がまだ無いときも理由を出す', () => {
    render(<FinalizeControl issues={[]} eventCount={0} busy={false} onFinalize={noop} />)
    expect(screen.getByRole('button', { name: '確定して実装へ' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('出来事')
  })

  it('押せるときは理由を出さず、押すと確定を呼ぶ（送信中は二重に押せない）', async () => {
    let resolve: () => void = () => {}
    const onFinalize = vi.fn(() => new Promise<void>((r) => (resolve = r)))
    render(<FinalizeControl issues={[]} eventCount={1} busy={false} onFinalize={onFinalize} />)
    const button = screen.getByRole('button', { name: '確定して実装へ' })
    expect(button).toBeEnabled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.click(button)
    expect(onFinalize).toHaveBeenCalledTimes(1)
    expect(button).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('確定しています')
    resolve()
    await waitFor(() => expect(button).toBeEnabled())
  })
})
