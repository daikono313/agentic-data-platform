import { Send } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import type { ChatMessage } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

interface Props {
  messages: ChatMessage[]
  draft: string
  question?: string | null
  choices: string[]
  busy: boolean
  disabled?: boolean
  onSend: (text: string) => void
}

export function ChatPanel({ messages, draft, question, choices, busy, disabled, onSend }: Props) {
  const [text, setText] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: 'end' })
  }, [messages.length, draft])

  const submit = (value: string) => {
    const v = value.trim()
    if (!v || busy || disabled) return
    onSend(v)
    setText('')
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3" role="log" aria-label="会話">
        {messages.map((m, i) => {
          // 今出ている選択肢の質問はボタンの上にだけ出し、吹き出しには出さない（二重表示を防ぐ）
          const activeQuestion = i === messages.length - 1 && !busy && choices.length > 0
          const showQuestion = m.role === 'assistant' && m.question && !activeQuestion
          return (
            <div key={i} className={cn('flex', m.role === 'user' ? 'justify-end' : 'justify-start')}>
              <div
                className={cn(
                  'max-w-[85%] space-y-2 whitespace-pre-wrap rounded-lg px-3 py-2 text-sm',
                  m.role === 'user' ? 'bg-primary text-primary-foreground' : 'bg-muted',
                )}
              >
                {m.text && <p>{m.text}</p>}
                {showQuestion && <p className="font-medium">{m.question}</p>}
              </div>
            </div>
          )
        })}
        {busy && (
          <div className="flex justify-start">
            <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-muted px-3 py-2 text-sm">
              {draft || <span className="text-muted-foreground">考えています…</span>}
            </p>
          </div>
        )}
        <div ref={bottom} />
      </div>
      {!busy && choices.length > 0 && (
        <div className="space-y-2 border-t p-3">
          {question && <p className="text-xs text-muted-foreground">{question}</p>}
          <div className="flex flex-wrap gap-2">
            {choices.map((c) => (
              <Button key={c} variant="outline" size="sm" onClick={() => submit(c)} disabled={disabled}>
                {c}
              </Button>
            ))}
          </div>
        </div>
      )}
      <form
        className="flex gap-2 border-t p-3"
        onSubmit={(e) => {
          e.preventDefault()
          submit(text)
        }}
      >
        <Input
          aria-label="回答を入力"
          placeholder={disabled ? '確定済みです' : '回答を入力'}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={busy || disabled}
        />
        <Button type="submit" size="icon" aria-label="送信" disabled={busy || disabled || !text.trim()}>
          <Send className="size-4" />
        </Button>
      </form>
    </div>
  )
}
