import { describe, expect, it } from 'vitest'

import { readSSE } from './client'

describe('readSSE', () => {
  it('分割されて届いたイベントを組み立てる', async () => {
    const chunks = ['event: text\ndata: {"text":"こん"}\n\n', 'event: done\nda', 'ta: {"message":{"role":"assistant","text":"x"}}\n\n']
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        for (const ch of chunks) c.enqueue(new TextEncoder().encode(ch))
        c.close()
      },
    })
    const events = []
    for await (const e of readSSE(new Response(body))) events.push(e)
    expect(events).toEqual([
      { event: 'text', data: { text: 'こん' } },
      { event: 'done', data: { message: { role: 'assistant', text: 'x' } } },
    ])
  })
})
