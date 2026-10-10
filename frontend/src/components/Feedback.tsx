import { Star, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useState } from 'react'

import { errorText } from '@/api/datasets'
import { MAX_COMMENT, type FeedbackCategories, type FeedbackIn, type FeedbackView } from '@/api/feedback'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export type SendFeedback = (body: FeedbackIn) => Promise<FeedbackView>

function useSender(send: SendFeedback) {
  const [status, setStatus] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const submit = async (body: FeedbackIn): Promise<boolean> => {
    setStatus('sending')
    try {
      await send(body)
      setStatus('sent')
      setError(null)
      return true
    } catch (e) {
      setStatus('error')
      setError(errorText(e))
      return false
    }
  }
  return { status, error, submit }
}

function Details({
  categories,
  initial,
  onSubmit,
  disabled,
}: {
  categories: FeedbackCategories
  initial?: FeedbackView
  onSubmit: (categories: string[], comment: string) => void
  disabled: boolean
}) {
  const [selected, setSelected] = useState<string[]>(initial?.categories ?? [])
  const [comment, setComment] = useState(initial?.comment ?? '')
  return (
    <form
      className="mt-2 space-y-2"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit(selected, comment)
      }}
    >
      <div className="flex flex-wrap gap-1" role="group" aria-label="気になった点">
        {Object.entries(categories).map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={selected.includes(key)}
            className={cn(
              'rounded-full border px-2 py-0.5 text-xs',
              selected.includes(key) ? 'border-primary bg-primary text-primary-foreground' : 'hover:bg-background',
            )}
            onClick={() => setSelected((s) => (s.includes(key) ? s.filter((x) => x !== key) : [...s, key]))}
          >
            {label}
          </button>
        ))}
      </div>
      <textarea
        aria-label="コメント"
        className="w-full rounded-md border bg-background p-2 text-xs"
        rows={2}
        maxLength={MAX_COMMENT}
        placeholder="（任意）どこがわかりにくかったか、どう聞いてほしかったか"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
      />
      <Button type="submit" size="sm" variant="outline" disabled={disabled}>
        送る
      </Button>
    </form>
  )
}

/** アシスタントの発言ごとの 👍 / 👎 と、分類・コメント（#44） */
export function MessageFeedback({
  index,
  current,
  categories,
  send,
}: {
  index: number
  current?: FeedbackView
  categories: FeedbackCategories
  send: SendFeedback
}) {
  const [thumb, setThumb] = useState<'up' | 'down' | null>(current?.thumb ?? null)
  const [open, setOpen] = useState(false)
  const { status, error, submit } = useSender(send)
  const choose = async (t: 'up' | 'down') => {
    // 👎 のときは理由を聞く。同じものを押し直したら、入力欄を開くだけにする
    setOpen(t === 'down')
    if (t === thumb) return
    const previous = thumb
    setThumb(t)
    // 評価だけを先に保存する（保存済みの分類・コメントは backend が残す）
    if (!(await submit({ kind: 'message', message_index: index, thumb: t }))) setThumb(previous)
  }
  return (
    <div className="text-xs text-muted-foreground">
      <div className="flex items-center gap-1">
        <button
          type="button"
          aria-label="この発言はよかった"
          aria-pressed={thumb === 'up'}
          className={cn('rounded p-1 hover:bg-background', thumb === 'up' && 'text-primary')}
          onClick={() => void choose('up')}
        >
          <ThumbsUp className="size-3.5" />
        </button>
        <button
          type="button"
          aria-label="この発言はよくなかった"
          aria-pressed={thumb === 'down'}
          className={cn('rounded p-1 hover:bg-background', thumb === 'down' && 'text-destructive')}
          onClick={() => void choose('down')}
        >
          <ThumbsDown className="size-3.5" />
        </button>
        {thumb && !open && (
          <button type="button" className="underline" onClick={() => setOpen(true)}>
            コメントする
          </button>
        )}
        {status === 'sent' && <span role="status">送信しました</span>}
        {error && (
          <span role="alert" className="text-destructive">
            {error}
          </span>
        )}
      </div>
      {open && thumb && (
        <Details
          categories={categories}
          initial={current}
          disabled={status === 'sending'}
          onSubmit={(cats, comment) => {
            setOpen(false)
            void submit({ kind: 'message', message_index: index, thumb, categories: cats, comment })
          }}
        />
      )}
    </div>
  )
}

/** セッション全体の評価（5 段階）とコメント（#44） */
export function SessionRating({ current, send }: { current?: FeedbackView; send: SendFeedback }) {
  const [score, setScore] = useState<number | null>(current?.score ?? null)
  const [comment, setComment] = useState(current?.comment ?? '')
  const { status, error, submit } = useSender(send)
  return (
    <form
      className="space-y-2"
      aria-label="このインタビューの評価"
      onSubmit={(e) => {
        e.preventDefault()
        if (score) void submit({ kind: 'session', score, comment })
      }}
    >
      <p className="text-xs font-semibold text-muted-foreground">このインタビューはどうでしたか？</p>
      <div className="flex gap-1" role="radiogroup" aria-label="評価">
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            type="button"
            role="radio"
            aria-checked={score === n}
            aria-label={`${n} 点`}
            onClick={() => setScore(n)}
            className="p-0.5"
          >
            <Star className={cn('size-5', score && n <= score ? 'fill-yellow-400 text-yellow-500' : 'text-muted-foreground')} />
          </button>
        ))}
      </div>
      <textarea
        aria-label="全体へのコメント"
        className="w-full rounded-md border bg-background p-2 text-xs"
        rows={2}
        maxLength={MAX_COMMENT}
        placeholder="（任意）質問の数・わかりやすさ・できあがったものについて"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
      />
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" variant="outline" disabled={!score || status === 'sending'}>
          評価を送る
        </Button>
        {status === 'sent' && (
          <span role="status" className="text-xs text-muted-foreground">
            ありがとうございます。改善に使います
          </span>
        )}
        {error && (
          <span role="alert" className="text-xs text-destructive">
            {error}
          </span>
        )}
      </div>
    </form>
  )
}
