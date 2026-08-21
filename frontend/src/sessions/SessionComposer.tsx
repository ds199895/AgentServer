import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'
import { Send, Square, X } from 'lucide-react'
import { agentApi } from '../agent/api'
import type { AgentSession } from '../agent/types'
import ApprovalBanner from './ApprovalBanner'
import ModelPicker, { type AgentModel, type ModelSelection } from './ModelPicker'

interface SessionComposerProps {
  session: AgentSession
  onSend: (input: string, options?: { model?: string | null; effort?: string | null }) => Promise<void>
}

/**
 * Model catalogue this session's provider reported when it started.
 *
 * Codex only answers `model/list` over a live app-server connection, so the
 * list arrives as a `session.models` event rather than from the device probe.
 * An empty list means the provider has no catalogue, and the picker offers
 * only the provider default.
 */
function availableModels(session: AgentSession): AgentModel[] {
  const models = (session.capabilities as { models?: unknown })?.models
  if (!Array.isArray(models)) return []
  return models.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return []
    const entry = raw as Record<string, unknown>
    const id = typeof entry.id === 'string' ? entry.id : null
    if (!id) return []
    return [{
      id,
      name: typeof entry.name === 'string' ? entry.name : id,
      isDefault: entry.default === true,
      efforts: Array.isArray(entry.efforts)
        ? entry.efforts.filter((value): value is string => typeof value === 'string')
        : [],
    }]
  })
}

export default function SessionComposer({ session, onSend }: SessionComposerProps) {
  const [input, setInput] = useState('')
  const [isComposing, setIsComposing] = useState(false)
  // P2.5: messages typed while the agent is busy wait here instead of being
  // rejected, so a train of thought is never blocked on the agent finishing.
  const [queue, setQueue] = useState<Array<{ text: string; selection: ModelSelection }>>([])
  const [selection, setSelection] = useState<ModelSelection>({ model: null, effort: null })
  const [sending, setSending] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const isBusy = session.state === 'running' || session.state === 'waiting'
  const isReady = session.state === 'ready'
  const canType = isReady || isBusy
  const canSubmit = canType && input.trim().length > 0 && !sending
  const canInterrupt = isBusy

  const pendingRequests = session.requests.filter((r) => r.status === 'pending')

  // Drain the queue as soon as the agent is idle again. `sending` guards
  // against a second drain starting before the first turn is accepted.
  useEffect(() => {
    if (!isReady || sending || queue.length === 0) return
    const [next, ...rest] = queue
    setSending(true)
    void (async () => {
      try {
        await onSend(next.text, next.selection)
        setQueue(rest)
      } finally {
        setSending(false)
      }
    })()
  }, [isReady, sending, queue, onSend])

  function resetHeight() {
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!canSubmit) return

    const text = input.trim()
    setInput('')
    resetHeight()

    if (isBusy) {
      setQueue((current) => [...current, { text, selection }])
      return
    }

    setSending(true)
    try {
      await onSend(text, selection)
    } finally {
      setSending(false)
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // P2.5: IME fix — `isComposing` keeps Enter from submitting mid-composition,
    // which otherwise sends a half-finished word for CJK input.
    if (e.key === 'Enter' && !e.shiftKey && !isComposing) {
      e.preventDefault()
      void handleSubmit(e)
    }
  }

  function handleInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value)
    // Auto-resize textarea
    e.target.style.height = 'auto'
    e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`
  }

  async function handleInterrupt() {
    if (!canInterrupt) return
    try {
      await agentApi.interrupt(session.id)
    } catch (error) {
      console.error('Failed to interrupt:', error)
    }
  }

  function removeQueued(index: number) {
    setQueue((current) => current.filter((_, i) => i !== index))
  }

  return (
    <div className="border-t border-border bg-card">
      {/* P1.3: Approval Banner - sticky above composer when requests pending */}
      {pendingRequests.length > 0 && <ApprovalBanner session={session} requests={pendingRequests} />}

      <form onSubmit={handleSubmit} className="p-4">
        {/* Queued messages waiting for the agent to finish its current turn. */}
        {queue.length > 0 && (
          <div className="mb-3 space-y-1.5">
            {queue.map((entry, index) => (
              <div
                key={index}
                className="flex items-start gap-2 px-3 py-2 bg-primary/10 border border-primary/30 rounded-md text-sm text-foreground"
              >
                <span className="text-xs font-medium text-primary mt-0.5 shrink-0">Queued</span>
                <span className="flex-1 min-w-0 break-words">{entry.text}</span>
                <button
                  type="button"
                  onClick={() => removeQueued(index)}
                  className="text-muted-foreground hover:text-foreground transition-colors shrink-0"
                  title="Remove from queue"
                  aria-label={`Remove queued message ${index + 1}`}
                >
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-3 items-end">
          <div className="flex-1">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={handleInput}
              onKeyDown={handleKeyDown}
              onCompositionStart={() => setIsComposing(true)}
              onCompositionEnd={() => setIsComposing(false)}
              placeholder={
                isReady
                  ? 'Type a message...'
                  : isBusy
                  ? 'Agent is working — your message will be queued'
                  : session.state === 'starting'
                  ? 'Starting...'
                  : `Session is ${session.state}`
              }
              disabled={!canType}
              rows={1}
              className="w-full px-4 py-3 border border-input rounded-lg resize-none focus:outline-none focus:ring-2 focus:ring-ring disabled:bg-secondary disabled:text-muted-foreground"
              style={{ minHeight: '52px', maxHeight: '200px' }}
            />
          </div>

          {/* Interrupt replaces send while a turn is in flight. */}
          {canInterrupt ? (
            <>
              <button
                type="submit"
                disabled={!canSubmit}
                className="px-4 py-3 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
                title="Queue message"
              >
                <Send size={18} />
                <span className="hidden sm:inline">Queue</span>
              </button>
              <button
                type="button"
                onClick={handleInterrupt}
                className="px-4 py-3 bg-destructive text-primary-foreground rounded-lg hover:bg-destructive/90 transition-colors flex items-center gap-2"
                title="Interrupt"
              >
                <Square size={18} />
                <span className="hidden sm:inline">Stop</span>
              </button>
            </>
          ) : (
            <button
              type="submit"
              disabled={!canSubmit}
              className="px-4 py-3 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              title="Send message"
            >
              <Send size={18} />
              <span className="hidden sm:inline">Send</span>
            </button>
          )}
        </div>

        <div className="mt-2 flex items-center gap-2">
          <ModelPicker
            models={availableModels(session)}
            sessionModel={session.model}
            selection={selection}
            onChange={setSelection}
            disabled={!canType}
          />
          {(selection.model || selection.effort) && (
            <button
              type="button"
              onClick={() => setSelection({ model: null, effort: null })}
              className="text-xs text-muted-foreground transition-colors hover:text-foreground"
            >
              Reset
            </button>
          )}
        </div>

        {session.state === 'failed' && session.turns.length > 0 && (
          <div className="mt-2 text-sm text-destructive">
            Last turn failed: {session.turns[session.turns.length - 1]?.error || 'Unknown error'}
          </div>
        )}
      </form>
    </div>
  )
}
