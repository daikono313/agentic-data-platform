import { describe, expect, it } from 'vitest'

import type { SessionView } from '@/api/types'

import { reducer, type State } from './App'

const session = {
  id: 's',
  phase: 'framing',
  beam: { name: 'draft', title: '下書き', request: 'r', events: [], metrics: [], gaps: [] },
  matrix: { columns: [], rows: [] },
  issues: ['x'],
  choices: ['a'],
  question: 'q',
  messages: [],
} as unknown as SessionView

const base: State = { session, draft: '', busy: false, error: null }

describe('reducer', () => {
  it('送信で選択肢を消し、応答を組み立てて done で確定する', () => {
    let s = reducer(base, { type: 'user', text: 'こんにちは' })
    expect(s.busy).toBe(true)
    expect(s.session?.choices).toEqual([])
    s = reducer(s, { type: 'event', event: { event: 'text', data: { text: '売上' } } })
    s = reducer(s, { type: 'event', event: { event: 'text', data: { text: 'ですね' } } })
    expect(s.draft).toBe('売上ですね')
    s = reducer(s, { type: 'event', event: { event: 'phase', data: { phase: 'seven_ws' } } })
    s = reducer(s, {
      type: 'event',
      event: { event: 'done', data: { message: { role: 'assistant', text: '売上ですね' } } },
    })
    expect(s.busy).toBe(false)
    expect(s.session?.phase).toBe('seven_ws')
    expect(s.session?.messages.map((m) => m.role)).toEqual(['user', 'assistant'])
  })

  it('beam イベントで成果物と課題を更新する', () => {
    const s = reducer(base, {
      type: 'event',
      event: {
        event: 'beam',
        data: { beam: { ...session.beam, title: '店舗別' }, matrix: session.matrix, issues: [] },
      },
    })
    expect(s.session?.beam.title).toBe('店舗別')
    expect(s.session?.issues).toEqual([])
  })

  it('done が届かずに終わっても idle で入力できる状態に戻す', () => {
    const s = reducer(reducer(base, { type: 'user', text: 'x' }), { type: 'idle' })
    expect(s.busy).toBe(false)
  })

  it('done のエラーを表示する', () => {
    const s = reducer(reducer(base, { type: 'user', text: 'x' }), {
      type: 'event',
      event: { event: 'done', data: { message: { role: 'assistant', text: '' }, error: '応答の途中です' } },
    })
    expect(s.error).toBe('応答の途中です')
    expect(s.session?.messages).toHaveLength(1)
  })
})
