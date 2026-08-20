import { Check, ChevronDown, ChevronRight, CircleAlert, CircleDot, LoaderCircle, Send, ShieldAlert, Square } from 'lucide-react'
import { useState } from 'react'
import { agentApi } from './api'
import { useAgentSession } from './useAgentSession'
import type { AgentActivity } from './types'

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

export function AgentSessionPane({ sessionId, onClose }: Props) {
  const { session, error, send } = useAgentSession(sessionId)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  if (!session) return <section className="flex min-h-0 flex-1 items-center justify-center bg-[#090d12] text-xs text-[#718590]">{error || 'Loading session…'}</section>
  const submit = async () => { const value = input.trim(); if (!value || sending) return; setSending(true); setInput(''); try { await send(value) } finally { setSending(false) } }
  return <section className="flex min-h-0 flex-1 flex-col bg-[#090d12] text-[#dce5eb]">
    <header className="flex items-center gap-3 border-b border-[#202b35] px-5 py-3"><div className="flex size-8 items-center justify-center rounded-md bg-[#173528] text-[#6ee7b7]"><CircleDot /></div><div><strong className="block text-sm">Agent Session</strong><small className="font-mono text-[10px] text-[#718590]">{session.provider} · {session.cwd}</small></div><span className="ml-auto rounded-full border border-[#315a48] px-2 py-1 text-[10px] text-[#6ee7b7]">{session.state}</span>{onClose && <button aria-label="Close session" onClick={onClose}><Square /></button>}</header>
    <div className="min-h-0 flex-1 overflow-auto px-5 py-5">
      {session.messages.map((message) => <div key={message.id} className={`mb-4 max-w-[88%] ${message.role === 'user' ? 'ml-auto' : ''}`}><div className="mb-1 text-[10px] uppercase tracking-[0.12em] text-[#718590]">{message.role}</div><div className={`rounded-md border px-3 py-2 text-sm whitespace-pre-wrap ${message.role === 'user' ? 'border-[#315a48] bg-[#14291f]' : 'border-[#26343e] bg-[#101820]'}`}>{message.text}</div></div>)}
      {session.activities.map((activity) => <ActivityRow key={activity.id} activity={activity} />)}
      {session.requests.filter((request) => request.status === 'pending').map((request) => <div key={request.id} className="my-4 flex items-start gap-3 rounded-md border border-[#735d2b] bg-[#211b0d] p-3 text-xs"><ShieldAlert className="mt-0.5 text-[#f3c969]" /><div className="min-w-0 flex-1"><strong className="block text-[#f3d88c]">{request.title}</strong><span className="mt-1 block text-[#ad9b6c]">{request.detail}</span><button className="mt-2 rounded border border-[#8b6f31] px-2 py-1 text-[10px]" onClick={() => void agentApi.respond(session.id, request.id, { decision: 'approve' })}>Approve</button></div></div>)}
    </div>
    <footer className="border-t border-[#202b35] p-4"><div className="flex items-end gap-2 rounded-md border border-[#2a3944] bg-[#0d141a] p-2"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }} placeholder="Send a message" rows={2} className="min-h-10 flex-1 resize-none bg-transparent px-2 py-1 text-sm outline-none" /><button aria-label="Send message" disabled={!input.trim() || sending} onClick={() => void submit()} className="flex size-8 items-center justify-center rounded bg-[#1e6b4c] text-white disabled:opacity-40"><Send /></button></div></footer>
  </section>
}
