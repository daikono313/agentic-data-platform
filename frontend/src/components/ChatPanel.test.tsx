import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ChatPanel } from './ChatPanel'

const base = { draft: '', busy: false, onSend: () => undefined }

describe('ChatPanel', () => {
  it('今出ている質問は、ボタンの上にだけ 1 回表示する', () => {
    render(
      <ChatPanel
        {...base}
        messages={[{ role: 'assistant', text: '支払方法は4種類です。', question: '支払方法別に見たいですか？' }]}
        question="支払方法別に見たいですか？"
        choices={['見たい', '見ない']}
      />,
    )
    expect(screen.getAllByText('支払方法別に見たいですか？')).toHaveLength(1)
    expect(screen.getByText('支払方法は4種類です。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '見たい' })).toBeInTheDocument()
  })

  it('過去の質問は吹き出しの中に表示する', () => {
    render(
      <ChatPanel
        {...base}
        messages={[
          { role: 'assistant', text: '前置き', question: '前の質問？' },
          { role: 'user', text: '見たい' },
        ]}
        choices={[]}
      />,
    )
    expect(screen.getAllByText('前の質問？')).toHaveLength(1)
  })

  it('本文が無く、質問がボタンの上に出ている発言は、空の吹き出しを出さない', () => {
    const { container } = render(
      <ChatPanel
        {...base}
        messages={[{ role: 'assistant', text: '', question: '優先度はどのくらいですか？' }]}
        question="優先度はどのくらいですか？"
        choices={['高い', '低い']}
      />,
    )
    expect(screen.getAllByText('優先度はどのくらいですか？')).toHaveLength(1)
    expect(container.querySelector('[role="log"] .bg-muted')).toBeNull()
  })

  it('本文が無い過去の発言は、質問を吹き出しに表示する', () => {
    render(
      <ChatPanel
        {...base}
        messages={[
          { role: 'assistant', text: '', question: '優先度はどのくらいですか？' },
          { role: 'user', text: '高い' },
        ]}
        choices={[]}
      />,
    )
    expect(screen.getAllByText('優先度はどのくらいですか？')).toHaveLength(1)
  })
})
