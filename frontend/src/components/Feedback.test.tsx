import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { mergeFeedback, type FeedbackIn, type FeedbackView } from '@/api/feedback'
import { ChatPanel } from '@/components/ChatPanel'
import { SessionRating } from '@/components/Feedback'

const CATEGORIES = { duplicate: '質問が重複している', jargon: '専門用語がわからない' }

function sender() {
  const sent: FeedbackIn[] = []
  const send = vi.fn(async (body: FeedbackIn): Promise<FeedbackView> => {
    sent.push(body)
    return {
      kind: body.kind,
      message_index: body.message_index ?? null,
      thumb: body.thumb ?? null,
      score: body.score ?? null,
      categories: body.categories ?? [],
      comment: body.comment ?? '',
      prompt_version: 'interview-v1+abc',
      model: 'm',
    }
  })
  return { sent, send }
}

describe('発言ごとのフィードバック', () => {
  const messages = [
    { role: 'user' as const, text: '店舗ごとの売上' },
    { role: 'assistant' as const, text: '粒度はどうしますか？' },
  ]

  it('アシスタントの発言にだけ評価ボタンを出し、👎 では理由を聞く', async () => {
    const { sent, send } = sender()
    render(
      <ChatPanel messages={messages} draft="" choices={[]} busy={false} onSend={() => {}} feedback={{ categories: CATEGORIES, send }} />,
    )
    expect(screen.getAllByLabelText('この発言はよくなかった')).toHaveLength(1)
    fireEvent.click(screen.getByLabelText('この発言はよくなかった'))
    expect(await screen.findByText('送信しました')).toBeInTheDocument()
    expect(sent[0]).toEqual({ kind: 'message', message_index: 1, thumb: 'down' })

    fireEvent.click(screen.getByRole('button', { name: '専門用語がわからない' }))
    fireEvent.change(screen.getByLabelText('コメント'), { target: { value: '『粒度』の意味がわからない' } })
    fireEvent.click(screen.getByRole('button', { name: '送る' }))
    await vi.waitFor(() => expect(sent).toHaveLength(2))
    expect(sent[1]).toEqual({
      kind: 'message',
      message_index: 1,
      thumb: 'down',
      categories: ['jargon'],
      comment: '『粒度』の意味がわからない',
    })
  })

  it('👍 はすぐ送り、理由の入力欄は開かない', async () => {
    const { sent, send } = sender()
    render(<ChatPanel messages={messages} draft="" choices={[]} busy={false} onSend={() => {}} feedback={{ categories: CATEGORIES, send }} />)
    fireEvent.click(screen.getByLabelText('この発言はよかった'))
    expect(await screen.findByText('送信しました')).toBeInTheDocument()
    expect(sent).toEqual([{ kind: 'message', message_index: 1, thumb: 'up' }])
    expect(screen.queryByLabelText('コメント')).toBeNull()
  })

  it('送れなかったら理由を出し、評価を元に戻す', async () => {
    const send = vi.fn(async () => {
      throw new Error(JSON.stringify({ detail: 'セッションがありません' }))
    })
    render(<ChatPanel messages={messages} draft="" choices={[]} busy={false} onSend={() => {}} feedback={{ categories: CATEGORIES, send }} />)
    fireEvent.click(screen.getByLabelText('この発言はよかった'))
    expect(await screen.findByRole('alert')).toHaveTextContent('セッションがありません')
    expect(screen.getByLabelText('この発言はよかった')).toHaveAttribute('aria-pressed', 'false')
  })

  it('同じ評価を押し直しても送り直さない（保存済みのコメントを上書きしない）', async () => {
    const { sent, send } = sender()
    const current = { kind: 'message' as const, message_index: 1, thumb: 'down' as const, score: null, categories: ['jargon'], comment: 'x', prompt_version: null, model: null }
    render(
      <ChatPanel
        messages={messages}
        draft=""
        choices={[]}
        busy={false}
        onSend={() => {}}
        feedback={{ items: [current], categories: CATEGORIES, send }}
      />,
    )
    fireEvent.click(screen.getByLabelText('この発言はよくなかった'))
    expect(screen.getByLabelText('コメント')).toHaveValue('x')
    expect(sent).toEqual([])
  })

  it('フィードバックが無ければ評価ボタンを出さない（デモ）', () => {
    render(<ChatPanel messages={messages} draft="" choices={[]} busy={false} onSend={() => {}} />)
    expect(screen.queryByLabelText('この発言はよかった')).toBeNull()
  })
})

describe('セッション全体の評価', () => {
  it('5 段階とコメントを送る', async () => {
    const { sent, send } = sender()
    render(<SessionRating send={send} />)
    expect(screen.getByRole('button', { name: '評価を送る' })).toBeDisabled()
    fireEvent.click(screen.getByRole('radio', { name: '4 点' }))
    fireEvent.change(screen.getByLabelText('全体へのコメント'), { target: { value: '質問が多かった' } })
    fireEvent.click(screen.getByRole('button', { name: '評価を送る' }))
    expect(await screen.findByText('ありがとうございます。改善に使います')).toBeInTheDocument()
    expect(sent).toEqual([{ kind: 'session', score: 4, comment: '質問が多かった' }])
  })
})

it('同じ発言のフィードバックは置き換える', () => {
  const base = { score: null, categories: [], comment: '', prompt_version: null, model: null }
  const a: FeedbackView = { ...base, kind: 'message', message_index: 1, thumb: 'up' }
  const b: FeedbackView = { ...base, kind: 'message', message_index: 1, thumb: 'down' }
  const c: FeedbackView = { ...base, kind: 'message', message_index: 3, thumb: 'up' }
  expect(mergeFeedback(mergeFeedback([a, c], b), b).map((f) => [f.message_index, f.thumb])).toEqual([
    [3, 'up'],
    [1, 'down'],
  ])
})
