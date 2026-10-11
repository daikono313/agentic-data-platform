import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, detailText, errorText, readSSE, STREAM_INTERRUPTED, trackJob } from './client'
import type { JobEvent, JobView } from './types'

afterEach(() => vi.restoreAllMocks())

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

describe('API のエラー', () => {
  it('FastAPI の detail を取り出して投げる（生の JSON を出さない）', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{"detail":"確定済みのセッションです"}', { status: 409, statusText: 'Conflict' }),
    )
    await expect(api.finalize('s')).rejects.toThrow(/^確定済みのセッションです$/)
  })

  it('SSE の開始で失敗しても detail を投げる', async () => {
    const res = new Response('{"detail":"ジョブ j がありません"}', { status: 404 })
    await expect(readSSE(res).next()).rejects.toThrow(/^ジョブ j がありません$/)
  })

  it('本文が空なら状態コードを出す', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 502, statusText: 'Bad Gateway' }))
    await expect(api.history()).rejects.toThrow('502 Bad Gateway')
  })

  it('detail が一覧（入力の検証エラー）なら msg をつなぐ。JSON でなければそのまま', () => {
    expect(detailText('{"detail":[{"msg":"必須です"},{"msg":"短すぎます"}]}')).toBe('必須です / 短すぎます')
    expect(detailText('Internal Server Error')).toBe('Internal Server Error')
    expect(detailText('{"other":1}')).toBe('{"other":1}')
  })

  it('errorText は Error: を付けず、detail だけにする', () => {
    expect(errorText(new Error('止まりました'))).toBe('止まりました')
    expect(errorText(new Error('{"detail":"データ一覧は利用できません"}'))).toBe('データ一覧は利用できません')
    expect(errorText('文字列')).toBe('文字列')
  })
})

describe('trackJob', () => {
  const initial: JobView = {
    id: 'j',
    status: 'running',
    steps: [
      { step: 'a', label: 'A', status: 'pending', detail: '' },
      { step: 'b', label: 'B', status: 'pending', detail: '' },
    ],
  }
  const stream = (events: JobEvent[], fail?: Error) =>
    vi.spyOn(api, 'jobEvents').mockImplementation(async function* () {
      for (const e of events) yield e
      if (fail) throw fail
    })

  it('step と done を当てて渡し、最後の状態を返す', async () => {
    stream([
      { event: 'step', data: { step: 'a', label: 'A', status: 'done', detail: 'ok' } },
      { event: 'done', data: { ...initial, status: 'succeeded', pr_url: 'u' } },
    ])
    const seen: JobView[] = []
    const last = await trackJob(initial, (j) => seen.push(j))
    expect(seen[0]).toBe(initial)
    expect(seen[1].steps.map((s) => s.status)).toEqual(['done', 'pending'])
    expect(last).toMatchObject({ status: 'succeeded', pr_url: 'u' })
    expect(seen.at(-1)).toBe(last)
  })

  it('done が届かずに終わったら、実行中のまま固めずに失敗にする', async () => {
    stream([{ event: 'step', data: { step: 'a', label: 'A', status: 'running', detail: '' } }])
    const seen: JobView[] = []
    const last = await trackJob(initial, (j) => seen.push(j))
    expect(last).toMatchObject({ status: 'failed', error: STREAM_INTERRUPTED })
    expect(last.steps[0].status).toBe('running')
    expect(seen.at(-1)).toBe(last)
  })

  it('通信が切れて例外になっても失敗にしてから投げ直す', async () => {
    stream([], new Error('network'))
    const seen: JobView[] = []
    await expect(trackJob(initial, (j) => seen.push(j), 'もう一度')).rejects.toThrow('network')
    expect(seen.at(-1)).toMatchObject({ status: 'failed', error: 'もう一度' })
  })
})
