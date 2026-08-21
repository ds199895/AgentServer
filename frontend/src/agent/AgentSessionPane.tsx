import {
  Brain, Check, ChevronDown, ChevronRight, CircleAlert, CircleDot, FileCode2,
  LoaderCircle, Search, Send, ShieldAlert, Sparkles, Square, TerminalSquare, Wrench,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { agentApi } from './api'
import { buildAgentTimeline } from './timeline'
import { useAgentSession } from './useAgentSession'
import type { AgentActivity, AgentMessage, AgentRequest } from './types'

type Props = { sessionId: string; onClose?: () => void }

function displayValue(value: unknown) {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, null, 2) } catch { return String(value) }
}

function RichText({ text }: { text: string }) {
  const parts = text.split(/(```[\s\S]*?```)/g)
  return <div className="text-sm leading-6 text-[#dce5eb]">
    {parts.map((part, index) => part.startsWith('```')
      ? <pre key={index} className="my-2 max-h-96 overflow-auto rounded-lg border border-[#26343e] bg-[#080c11] p-3 font-mono text-xs whitespace-pre-wrap text-[#b8c7cf]">{part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '')}</pre>
      : <span key={index} className="whitespace-pre-wrap">{part}</span>)}
  </div>
}

function statusIcon(status: string) {
  if (['completed', 'succeeded', 'success'].includes(status)) return <Check className="size-3.5" />
  if (['failed', 'error'].includes(status)) return <CircleAlert className="size-3.5" />
  if (['cancelled', 'canceled', 'interrupted', 'declined'].includes(status)) return <Square className="size-3.5" />
  return <LoaderCircle className="size-3.5 animate-spin" />
}

function activityIcon(activity: AgentActivity) {
  if (activity.kind === 'command') return <TerminalSquare className="size-4" />
  if (activity.kind === 'file') return <FileCode2 className="size-4" />
  if (activity.kind === 'plan') return <Sparkles className="size-4" />
  if (activity.title.toLowerCase().includes('search')) return <Search className="size-4" />
  if (activity.kind === 'status') return <Brain className="size-4" />
  return <Wrench className="size-4" />
}

function DataBlock({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null || value === '') return null
  return <div className="mt-2">
    <div className="mb-1 font-mono text-[9px] uppercase tracking-[0.14em] text-[#607682]">{label}</div>
    <pre className="max-h-72 overflow-auto rounded-lg border border-[#1d2931] bg-[#080c11] p-3 font-mono text-[11px] leading-5 whitespace-pre-wrap break-words text-[#aebdc5]">{displayValue(value)}</pre>
  </div>
}

function PlanSteps({ value }: { value: unknown }) {
  if (!Array.isArray(value)) return <DataBlock label="Plan" value={value} />
  return <ol className="mt-2 grid gap-1.5">
    {value.map((raw, index) => {
      const item: Record<string, unknown> = raw && typeof raw === 'object' ? raw as Record<string, unknown> : { step: raw }
      const done = item.status === 'completed' || item.status === 'succeeded'
      return <li key={index} className="flex gap-2 text-xs text-[#aebdc5]">
        <span className={`mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border text-[9px] ${done ? 'border-[#315a48] text-[#6ee7b7]' : 'border-[#3a4b56] text-[#718590]'}`}>{done ? '✓' : index + 1}</span>
        <span>{String(item.step ?? item.title ?? '')}</span>
      </li>
    })}
  </ol>
}

function ActivityRow({ activity }: { activity: AgentActivity }) {
  const failed = ['failed', 'error'].includes(activity.status)
  const running = ['running', 'in_progress', 'inProgress'].includes(activity.status)
  const [open, setOpen] = useState(failed || running || activity.collapsed === false)
  const manuallyToggled = useRef(false)
  const previousStatus = useRef(activity.status)
  useEffect(() => {
    const wasRunning = ['running', 'in_progress', 'inProgress'].includes(previousStatus.current)
    if (failed) setOpen(true)
    else if (wasRunning && !running && !manuallyToggled.current) setOpen(false)
    previousStatus.current = activity.status
  }, [activity.status, failed, running])
  const expandable = Boolean(activity.detail || activity.input || activity.output || activity.kind === 'plan')
  return <div className="group relative pl-8">
    <span className="absolute left-[7px] top-4 flex size-5 items-center justify-center rounded-full border border-[#2b3a44] bg-[#101820] text-[#7dd3b0]">{activityIcon(activity)}</span>
    <div className={`rounded-xl border bg-[#0d141a] ${failed ? 'border-[#713640]' : 'border-[#202e37]'}`}>
      <button disabled={!expandable} className="flex w-full items-center gap-2 px-3 py-2.5 text-left disabled:cursor-default" onClick={() => { if (expandable) { manuallyToggled.current = true; setOpen(!open) } }}>
        {expandable ? (open ? <ChevronDown className="size-3.5 text-[#718590]" /> : <ChevronRight className="size-3.5 text-[#718590]" />) : <span className="w-3.5" />}
        <strong className="truncate text-xs font-medium text-[#c7d4dc]">{activity.title}</strong>
        {!open && activity.detail && <span className="truncate text-[11px] text-[#718590]">{activity.detail}</span>}
        <span className={`ml-auto flex shrink-0 items-center gap-1 font-mono text-[9px] uppercase tracking-wide ${failed ? 'text-[#ff8290]' : running ? 'text-[#f3c969]' : 'text-[#6ee7b7]'}`}>{statusIcon(activity.status)}{activity.status.replace('_', ' ')}</span>
      </button>
      {open && <div className="border-t border-[#1d2931] px-3 pb-3 pt-1">
        {activity.detail && <p className="mt-2 text-xs leading-5 text-[#91a4af]">{activity.detail}</p>}
        {activity.kind === 'plan' ? <PlanSteps value={activity.input} /> : <DataBlock label="Input" value={activity.input} />}
        <DataBlock label="Output" value={activity.output} />
      </div>}
    </div>
  </div>
}

function ReasoningRow({ message }: { message: AgentMessage }) {
  const [open, setOpen] = useState(false)
  return <div className="relative pl-8">
    <span className="absolute left-[7px] top-2 flex size-5 items-center justify-center rounded-full border border-[#34404a] bg-[#111820] text-[#9aaab3]"><Brain className="size-3.5" /></span>
    <button className="flex max-w-full items-center gap-1.5 py-2 text-left text-xs text-[#91a4af]" onClick={() => setOpen(!open)}>
      {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
      <span>{message.streaming ? 'Thinking…' : 'Thought process'}</span>
      {!open && <span className="truncate text-[#607682]">{message.text}</span>}
    </button>
    {open && <div className="mb-1 rounded-lg border border-[#202e37] bg-[#0b1117] px-3 py-2 text-xs leading-5 whitespace-pre-wrap text-[#91a4af]">{message.text}</div>}
  </div>
}

function MessageRow({ message }: { message: AgentMessage }) {
  if (message.role === 'reasoning') return <ReasoningRow message={message} />
  const user = message.role === 'user'
  return <div className={`relative pl-8 ${user ? 'flex justify-end' : ''}`}>
    {!user && <span className="absolute left-[7px] top-3 flex size-5 items-center justify-center rounded-full border border-[#315a48] bg-[#14291f] text-[#6ee7b7]"><Sparkles className="size-3.5" /></span>}
    <div className={user ? 'max-w-[85%] rounded-2xl rounded-br-md border border-[#315a48] bg-[#14291f] px-4 py-2.5' : 'min-w-0 max-w-full py-2'}>
      {!user && <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-[#607682]">Assistant</div>}
      <RichText text={message.text} />
      {message.streaming && <span className="mt-1 inline-block size-1.5 animate-pulse rounded-full bg-[#6ee7b7]" />}
    </div>
  </div>
}

function RequestCard({ request, sessionId, reload }: { request: AgentRequest; sessionId: string; reload: () => Promise<void> }) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [responding, setResponding] = useState(false)
  const respond = async (payload: Record<string, unknown>) => {
    setResponding(true)
    try { await agentApi.respond(sessionId, request.id, payload); await reload() } finally { setResponding(false) }
  }
  const pending = request.status === 'pending'
  return <div className="relative pl-8">
    <span className="absolute left-[7px] top-4 flex size-5 items-center justify-center rounded-full border border-[#735d2b] bg-[#211b0d] text-[#f3c969]"><ShieldAlert className="size-3.5" /></span>
    <div className={`rounded-xl border p-3 text-xs ${pending ? 'border-[#735d2b] bg-[#17140d]' : 'border-[#2a3944] bg-[#0d141a]'}`}>
      <div className="flex items-center gap-2"><strong className={pending ? 'text-[#f3d88c]' : 'text-[#aebdc5]'}>{request.title}</strong><span className="ml-auto font-mono text-[9px] uppercase text-[#718590]">{request.status}</span></div>
      {request.detail && <span className="mt-1 block leading-5 text-[#ad9b6c]">{request.detail}</span>}
      <DataBlock label="Request" value={request.input} />
      {request.kind === 'user_input' && pending ? <>
        <div className="mt-3 grid gap-3">
          {request.options.map((question, index) => {
            const id = String(question.id || `question-${index + 1}`)
            const values = Array.isArray(question.options) ? question.options : []
            return <label key={id} className="grid gap-1 text-[#d8c995]">{String(question.question || question.header || id)}
              {values.length ? <select value={answers[id] || ''} onChange={(event) => setAnswers((current) => ({ ...current, [id]: event.target.value }))} className="rounded border border-[#735d2b] bg-[#100d07] px-2 py-1.5 text-[#eee3ba]"><option value="">Select…</option>{values.map((option, optionIndex) => <option key={optionIndex} value={String((option as Record<string, unknown>).label || '')}>{String((option as Record<string, unknown>).label || '')}</option>)}</select> : <input value={answers[id] || ''} onChange={(event) => setAnswers((current) => ({ ...current, [id]: event.target.value }))} className="rounded border border-[#735d2b] bg-[#100d07] px-2 py-1.5 text-[#eee3ba]" />}
            </label>
          })}
        </div>
        <button disabled={responding || !request.options.length || request.options.some((question, index) => !answers[String(question.id || `question-${index + 1}`)])} className="mt-3 rounded border border-[#8b6f31] px-2 py-1 text-[10px] disabled:opacity-40" onClick={() => void respond({ answers })}>Submit answers</button>
      </> : request.kind === 'approval' && pending ? <div className="mt-3 flex gap-2"><button disabled={responding} className="rounded border border-[#8b6f31] px-2 py-1 text-[10px] disabled:opacity-40" onClick={() => void respond({ decision: 'approve_once' })}>Approve once</button><button disabled={responding} className="rounded border border-[#713640] px-2 py-1 text-[10px] text-[#ffadb5] disabled:opacity-40" onClick={() => void respond({ decision: 'deny' })}>Deny</button></div> : request.response !== undefined && request.response !== null && <div className="mt-2 font-mono text-[10px] text-[#718590]">Resolution: {displayValue(request.response)}</div>}
    </div>
  </div>
}

export function AgentSessionPane({ sessionId, onClose }: Props) {
  const { session, error, reload, send } = useAgentSession(sessionId)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const timeline = useMemo(() => session ? buildAgentTimeline(session) : [], [session])
  useEffect(() => {
    if (followRef.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [timeline, session?.state])
  if (!session) return <section className="flex min-h-0 flex-1 items-center justify-center bg-[#090d12] text-xs text-[#718590]">{error || 'Loading session…'}</section>
  const canSend = session.state === 'ready'
  const submit = async () => {
    const value = input.trim()
    if (!value || sending || !canSend) return
    setSending(true); setSendError('')
    try { await send(value); setInput(''); followRef.current = true } catch (reason) { setSendError(reason instanceof Error ? reason.message : 'Message failed') } finally { setSending(false) }
  }
  return <section className="flex min-h-0 flex-1 flex-col bg-[#090d12] text-[#dce5eb]">
    <header className="flex items-center gap-3 border-b border-[#202b35] px-5 py-3"><div className="flex size-8 items-center justify-center rounded-md bg-[#173528] text-[#6ee7b7]"><CircleDot /></div><div className="min-w-0"><strong className="block text-sm">Agent Session</strong><small className="block truncate font-mono text-[10px] text-[#718590]">{session.provider} · {session.device_id} · {session.cwd}</small></div><span className="ml-auto rounded-full border border-[#315a48] px-2 py-1 text-[10px] text-[#6ee7b7]">{session.state}</span>{['running', 'waiting'].includes(session.state) && <button className="rounded border border-[#713640] px-2 py-1 text-[10px] text-[#ffadb5]" onClick={() => void agentApi.interrupt(session.id).then(reload)}>Interrupt</button>}{onClose && <button aria-label="Close session" onClick={onClose}><Square /></button>}</header>
    <div ref={scrollRef} onScroll={(event) => { const target = event.currentTarget; followRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 80 }} className="min-h-0 flex-1 overflow-auto px-5 py-5">
      <div className="relative mx-auto grid max-w-4xl gap-3 before:absolute before:bottom-3 before:left-4 before:top-3 before:w-px before:bg-[#1d2931]">
        {timeline.map((item) => item.type === 'message' ? <MessageRow key={item.key} message={item.value} /> : item.type === 'activity' ? <ActivityRow key={item.key} activity={item.value} /> : <RequestCard key={item.key} request={item.value} sessionId={session.id} reload={reload} />)}
        {!timeline.length && <div className="py-16 text-center text-xs text-[#718590]">Send a message to start this agent.</div>}
      </div>
    </div>
    <footer className="border-t border-[#202b35] p-4">{(error || sendError) && <p className="mb-2 text-xs text-[#ff8290]">{sendError || error}</p>}<div className="mx-auto flex max-w-4xl items-end gap-2 rounded-xl border border-[#2a3944] bg-[#0d141a] p-2 shadow-lg"><textarea value={input} disabled={!canSend} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }} placeholder={canSend ? 'Send a message' : `Agent is ${session.state}`} rows={2} className="min-h-10 flex-1 resize-none bg-transparent px-2 py-1 text-sm outline-none disabled:opacity-50" /><button aria-label="Send message" disabled={!input.trim() || sending || !canSend} onClick={() => void submit()} className="flex size-8 items-center justify-center rounded-lg bg-[#1e6b4c] text-white disabled:opacity-40"><Send className="size-4" /></button></div></footer>
  </section>
}
