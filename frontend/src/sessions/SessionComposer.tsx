import { FormEvent, KeyboardEvent, useRef, useState } from 'react'
import { Send, Square } from 'lucide-react'
import type { AgentSession } from '../agent/types'
import ApprovalBanner from './ApprovalBanner'

interface SessionComposerProps {
  session: AgentSession
  onSend: (input: string) => Promise<void>
}

export default function SessionComposer({ session, onSend }: SessionComposerProps) {
  const [input, setInput] = useState('')
  const [isComposing, setIsComposing] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const canSend = session.state === 'ready' && input.trim().length > 0
  const canInterrupt = session.state === 'running' || session.state === 'waiting'

  const pendingRequests = session.requests.filter((r) => r.status === 'pending')

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!canSend) return

    const text = input.trim()
    setInput('')
    await onSend(text)

    // Reset textarea height after send
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // P2.5: IME bug fix - check isComposing to prevent premature sends during CJK input
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
      await fetch(`/api/agent/sessions/${session.id}/interrupt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
    } catch (error) {
      console.error('Failed to interrupt:', error)
    }
  }

  return (
    <div className="border-t border-gray-200 bg-white">
      {/* P1.3: Approval Banner - sticky above composer when requests pending */}
      {pendingRequests.length > 0 && <ApprovalBanner session={session} requests={pendingRequests} />}

      <form onSubmit={handleSubmit} className="p-4">
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
                session.state === 'ready'
                  ? 'Type a message...'
                  : session.state === 'starting'
                  ? 'Starting...'
                  : 'Agent is working...'
              }
              disabled={session.state !== 'ready'}
              rows={1}
              className="w-full px-4 py-3 border border-gray-300 rounded-lg resize-none focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50 disabled:text-gray-500"
              style={{ minHeight: '52px', maxHeight: '200px' }}
            />
          </div>

          {/* P2.5: Interrupt button when agent is running */}
          {canInterrupt && (
            <button
              type="button"
              onClick={handleInterrupt}
              className="px-4 py-3 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors flex items-center gap-2"
              title="Interrupt"
            >
              <Square size={18} />
              <span className="hidden sm:inline">Stop</span>
            </button>
          )}

          {/* Send button */}
          {!canInterrupt && (
            <button
              type="submit"
              disabled={!canSend}
              className="px-4 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              title="Send message"
            >
              <Send size={18} />
              <span className="hidden sm:inline">Send</span>
            </button>
          )}
        </div>

        {session.state === 'failed' && session.turns.length > 0 && (
          <div className="mt-2 text-sm text-red-600">
            Last turn failed: {session.turns[session.turns.length - 1]?.error || 'Unknown error'}
          </div>
        )}
      </form>
    </div>
  )
}
