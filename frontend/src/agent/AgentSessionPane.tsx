import { Check, ChevronDown, ChevronRight, CircleAlert, CircleDot, LoaderCircle, Send, ShieldAlert, Square } from 'lucide-react'
import { useState } from 'react'
import { agentApi } from './api'
import { useAgentSession } from './useAgentSession'
import type { AgentActivity, AgentRequest } from './types'

type Props = { sessionId: string; onClose?: () => void }

function ActivityRow({ activity }: { activity: AgentActivity }) {
  const [open, setOpen] = useState(!activity.collapsed)
  const icon = activity.status === 'completed' ? <Check /> : activity.status === 'failed' ? <CircleAlert /> : <LoaderCircle className="animate-spin" />
  return <div className="border-b border-[#202b35] px-4 py-3 text-xs">
    <button className="flex w-full items-center gap-2 text-left text-[#c7d4dc]" onClick={() => setOpen(!open)}>
      {open ? <ChevronDown /> : <ChevronRight />} <span className="text-[#6ee7b7]">{icon}</span><strong>{activity.title}</strong><span className="ml-auto font-mono text-[10px] text-[#718590]">{activity.status}</span>
    </button>
    {open && Boolean(activity.detail || activity.output) && <pre className="mt-2 max-h-40 overflow-auto rounded-md bg-[#080c11] p-2 font-mono text-[10px] whitespace-pre-wrap text-[#91a4af]">{activity.detail || String(JSON.stringify(activity.output, null, 2))}</pre>}
  </div>
}

function RequestCard({ request, sessionId, reload }: { request: AgentRequest; sessionId: string; reload: () => Promise<void> }) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const respond = async (payload: Record<string, unknown>) => {
    await agentApi.respond(sessionId, request.id, payload)
    await reload()
  }
  return <div className="my-4 flex items-start gap-3 rounded-md border border-[#735d2b] bg-[#211b0d] p-3 text-xs">
    <ShieldAlert className="mt-0.5 text-[#f3c969]" />
    <div className="min-w-0 flex-1">
      <strong className="block text-[#f3d88c]">{request.title}</strong>
      <span className="mt-1 block text-[#ad9b6c]">{request.detail}</span>
      {request.kind === 'user_input' ? <>
        <div className="mt-3 grid gap-3">
          {request.options.map((question, index) => {
            const id = String(question.id || `question-${index + 1}`)
            const values = Array.isArray(question.options) ? question.options : []
            return <label key={id} className="grid gap-1 text-[#d8c995]">
              {String(question.question || question.header || id)}
              {values.length ? <select value={answers[id] || ''} onChange={(event) => setAnswers((current) => ({ ...current, [id]: event.target.value }))} className="rounded border border-[#735d2b] bg-[#100d07] px-2 py-1.5 text-[#eee3ba]">
                <option value="">Select…</option>
                {values.map((option, optionIndex) => <option key={optionIndex} value={String((option as Record<string, unknown>).label || '')}>{String((option as Record<string, unknown>).label || '')}</option>)}
              </select> : <input value={answers[id] || ''} onChange={(event) => setAnswers((current) => ({ ...current, [id]: event.target.value }))} className="rounded border border-[#735d2b] bg-[#100d07] px-2 py-1.5 text-[#eee3ba]" />}
            </label>
          })}
        </div>
        <button disabled={!request.options.length || request.options.some((question, index) => !answers[String(question.id || `question-${index + 1}`)])} className="mt-3 rounded border border-[#8b6f31] px-2 py-1 text-[10px] disabled:opacity-40" onClick={() => void respond({ answers })}>Submit answers</button>
      </> : <div className="mt-2 flex gap-2">
        <button className="rounded border border-[#8b6f31] px-2 py-1 text-[10px]" onClick={() => void respond({ decision: 'approve_once' })}>Approve once</button>
        <button className="rounded border border-[#713640] px-2 py-1 text-[10px] text-[#ffadb5]" onClick={() => void respond({ decision: 'deny' })}>Deny</button>
      </div>}
    </div>
  </div>
}

export function AgentSessionPane({ sessionId, onClose }: Props) {
  const { session, error, reload, send } = useAgentSession(sessionId)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState('')
  if (!session) return <section className="flex min-h-0 flex-1 items-center justify-center bg-[#090d12] text-xs text-[#718590]">{error || 'Loading session…'}</section>
  const canSend = session.state === 'ready'
  const submit = async () => {
    const value = input.trim()
    if (!value || sending || !canSend) return
    setSending(true)
    setSendError('')
    try {
      await send(value)
      setInput('')
    } catch (reason) {
      setSendError(reason instanceof Error ? reason.message : 'Message failed')
    } finally {
      setSending(false)
    }
  }
  return <section className="flex min-h-0 flex-1 flex-col bg-[#090d12] text-[#dce5eb]">
    <header className="flex items-center gap-3 border-b border-[#202b35] px-5 py-3"><div className="flex size-8 items-center justify-center rounded-md bg-[#173528] text-[#6ee7b7]"><CircleDot /></div><div><strong className="block text-sm">Agent Session</strong><small className="font-mono text-[10px] text-[#718590]">{session.provider} · {session.device_id} · {session.cwd}</small></div><span className="ml-auto rounded-full border border-[#315a48] px-2 py-1 text-[10px] text-[#6ee7b7]">{session.state}</span>{['running', 'waiting'].includes(session.state) && <button className="rounded border border-[#713640] px-2 py-1 text-[10px] text-[#ffadb5]" onClick={() => void agentApi.interrupt(session.id).then(reload)}>Interrupt</button>}{onClose && <button aria-label="Close session" onClick={onClose}><Square /></button>}</header>
    <div className="min-h-0 flex-1 overflow-auto px-5 py-5">
      {session.messages.map((message) => <div key={message.id} className={`mb-4 max-w-[88%] ${message.role === 'user' ? 'ml-auto' : ''}`}><div className="mb-1 text-[10px] uppercase tracking-[0.12em] text-[#718590]">{message.role}</div><div className={`rounded-md border px-3 py-2 text-sm whitespace-pre-wrap ${message.role === 'user' ? 'border-[#315a48] bg-[#14291f]' : 'border-[#26343e] bg-[#101820]'}`}>{message.text}</div></div>)}
      {session.activities.map((activity) => <ActivityRow key={activity.id} activity={activity} />)}
      {session.requests.filter((request) => request.status === 'pending').map((request) => <RequestCard key={request.id} request={request} sessionId={session.id} reload={reload} />)}
    </div>
    <footer className="border-t border-[#202b35] p-4">{(error || sendError) && <p className="mb-2 text-xs text-[#ff8290]">{sendError || error}</p>}<div className="flex items-end gap-2 rounded-md border border-[#2a3944] bg-[#0d141a] p-2"><textarea value={input} disabled={!canSend} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }} placeholder={canSend ? 'Send a message' : `Agent is ${session.state}`} rows={2} className="min-h-10 flex-1 resize-none bg-transparent px-2 py-1 text-sm outline-none disabled:opacity-50" /><button aria-label="Send message" disabled={!input.trim() || sending || !canSend} onClick={() => void submit()} className="flex size-8 items-center justify-center rounded bg-[#1e6b4c] text-white disabled:opacity-40"><Send /></button></div></footer>
  </section>
}
