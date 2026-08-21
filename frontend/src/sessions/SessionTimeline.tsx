import { useMemo, useRef, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { ChevronDown, ChevronRight, Brain, Terminal, CheckCircle, XCircle, Clock, Check, Copy, FileCode2, Wrench } from 'lucide-react'
import { buildAgentTimeline, type AgentTimelineItem } from '../agent/timeline'
import type { AgentSession, AgentActivity } from '../agent/types'

interface SessionTimelineProps {
  session: AgentSession
}

interface TurnGroup {
  turnId: string
  userMessage: string
  entries: AgentTimelineItem[]
  startedAt: number
  completedAt: number | null
  status: 'running' | 'completed' | 'failed' | 'interrupted'
}

/**
 * Fold the flat timeline into one card per turn.
 *
 * Entries without a `turn_id` (session-level warnings, provider errors raised
 * before a turn started) are attached to the turn in progress, or to a
 * standalone leading group when there is none — dropping them would silently
 * hide exactly the diagnostics a reader needs when something went wrong.
 */
function groupByTurn(entries: AgentTimelineItem[], session: AgentSession): TurnGroup[] {
  const groups: TurnGroup[] = []
  const turnMap = new Map<string, TurnGroup>()
  let current: TurnGroup | null = null

  const ensureGroup = (turnId: string, entry: AgentTimelineItem): TurnGroup => {
    const existing = turnMap.get(turnId)
    if (existing) return existing
    const turn = session.turns.find((t) => t.id === turnId)
    const userMsg = session.messages.find((m) => m.role === 'user' && m.turn_id === turnId)
    const group: TurnGroup = {
      turnId,
      userMessage: userMsg?.text || turn?.input || '',
      entries: [],
      startedAt: turn?.created_at || entry.createdAt,
      completedAt: turn?.completed_at ?? null,
      status:
        turn?.state === 'completed'
          ? 'completed'
          : turn?.state === 'failed'
          ? 'failed'
          : turn?.state === 'interrupted'
          ? 'interrupted'
          : 'running',
    }
    turnMap.set(turnId, group)
    groups.push(group)
    return group
  }

  for (const entry of entries) {
    const turnId = entry.value.turn_id
    if (turnId) {
      current = ensureGroup(turnId, entry)
      current.entries.push(entry)
      continue
    }
    // Unattributed entry: keep it with the turn it interleaved with, or open a
    // session-level group if nothing has started yet.
    const target: TurnGroup = current ?? ensureGroup('__session__', entry)
    current = target
    target.entries.push(entry)
  }

  return groups
}

/**
 * Both timestamps are epoch **seconds** (the server sends `time.time()`), so
 * they are converted here rather than mixed with `Date.now()` milliseconds.
 * A running turn measures against now, which is why this re-renders on a tick.
 */
function formatElapsed(startSeconds: number, endSeconds: number | null): string {
  const endMs = endSeconds !== null ? endSeconds * 1000 : Date.now()
  const elapsed = Math.max(0, endMs - startSeconds * 1000)
  if (elapsed < 1000) return `${Math.round(elapsed)}ms`
  if (elapsed < 60000) return `${(elapsed / 1000).toFixed(1)}s`
  return `${Math.floor(elapsed / 60000)}m ${Math.floor((elapsed % 60000) / 1000)}s`
}

/** Re-render once a second while a turn is still running, so its timer ticks. */
function useTick(active: boolean) {
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => window.clearInterval(id)
  }, [active])
}

function CodeBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard is unavailable (insecure origin or denied permission); the
      // text stays selectable, so failing quietly is better than an alert.
    }
  }
  return (
    <div className="group relative">
      <pre className="overflow-x-auto rounded border border-border bg-background p-2 text-xs text-foreground">
        {text}
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute right-1.5 top-1.5 rounded border border-border bg-card px-1.5 py-1 text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100 focus:opacity-100"
        title={copied ? 'Copied' : 'Copy'}
        aria-label="Copy output"
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </button>
    </div>
  )
}

function RichMarkdown({ content }: { content: string }) {
  return (
    <div className="agent-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight as any]}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

const RUNNING_STATUSES = ['running', 'in_progress', 'inProgress', 'pending']

function activityIcon(activity: AgentActivity) {
  if (activity.kind === 'command') return <Terminal size={14} />
  if (activity.kind === 'file') return <FileCode2 size={14} />
  if (activity.kind === 'plan') return <Clock size={14} />
  if (activity.kind === 'status') return <Brain size={14} />
  return <Wrench size={14} />
}

function statusIcon(status: string) {
  if (['succeeded', 'completed', 'success'].includes(status)) {
    return <CheckCircle size={14} className="text-primary" />
  }
  if (['failed', 'error'].includes(status)) {
    return <XCircle size={14} className="text-destructive" />
  }
  if (RUNNING_STATUSES.includes(status)) {
    return <Clock size={14} className="animate-spin text-warning" />
  }
  return null
}

function PlanSteps({ plan }: { plan: unknown }) {
  if (!Array.isArray(plan)) return null
  return (
    <ol className="mt-1 space-y-1">
      {plan.map((raw, index) => {
        const step: Record<string, unknown> =
          raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : { step: raw }
        const status = String(step.status ?? '')
        const done = status === 'completed' || status === 'succeeded'
        const active = status === 'in_progress' || status === 'inProgress'
        return (
          <li key={index} className="flex items-start gap-2 text-xs">
            <span
              className={`mt-px flex size-4 shrink-0 items-center justify-center rounded-full border text-[9px] ${
                done
                  ? 'border-primary text-primary'
                  : active
                  ? 'border-warning text-warning'
                  : 'border-border text-muted-foreground'
              }`}
            >
              {done ? '✓' : index + 1}
            </span>
            <span className={done ? 'text-muted-foreground line-through' : 'text-foreground'}>
              {String(step.step ?? step.title ?? '')}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/**
 * One compact row per activity. Collapsed by default so a turn with a dozen
 * tool calls stays readable; failures and in-flight work open themselves
 * because that is what the reader needs without hunting for it.
 */
function ActivityRow({ activity }: { activity: AgentActivity }) {
  const failed = ['failed', 'error'].includes(activity.status)
  const running = RUNNING_STATUSES.includes(activity.status)
  const isPlan = activity.kind === 'plan'
  const [open, setOpen] = useState(failed || running || isPlan)

  const outputText = activity.output
    ? typeof activity.output === 'string'
      ? activity.output
      : JSON.stringify(activity.output, null, 2)
    : null
  const inputText =
    activity.input && !isPlan
      ? typeof activity.input === 'string'
        ? activity.input
        : JSON.stringify(activity.input, null, 2)
      : null
  const expandable = Boolean(outputText || inputText || (isPlan && Array.isArray(activity.input)))

  return (
    <div className={`rounded-md border ${failed ? 'border-destructive/40' : 'border-border'} bg-secondary`}>
      <button
        type="button"
        disabled={!expandable}
        onClick={() => expandable && setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-left disabled:cursor-default"
      >
        {expandable ? (
          open ? (
            <ChevronDown size={13} className="shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight size={13} className="shrink-0 text-muted-foreground" />
          )
        ) : (
          <span className="w-[13px] shrink-0" />
        )}
        <span className="shrink-0 text-muted-foreground">{activityIcon(activity)}</span>
        <span className="truncate text-xs font-medium text-foreground">{activity.title}</span>
        {activity.detail && (
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {String(activity.detail)}
          </span>
        )}
        <span className="ml-auto shrink-0">{statusIcon(activity.status)}</span>
      </button>

      {open && expandable && (
        <div className="space-y-2 border-t border-border px-2.5 pb-2.5 pt-2">
          {isPlan && <PlanSteps plan={activity.input} />}
          {inputText && <CodeBlock text={inputText} />}
          {outputText && <CodeBlock text={outputText} />}
        </div>
      )}
    </div>
  )
}

function TurnCard({ group }: { group: TurnGroup }) {
  const running = group.status === 'running'
  useTick(running)

  const turnIcon =
    group.status === 'completed' ? (
      <CheckCircle size={16} className="text-primary" />
    ) : group.status === 'failed' ? (
      <XCircle size={16} className="text-destructive" />
    ) : group.status === 'interrupted' ? (
      <XCircle size={16} className="text-muted-foreground" />
    ) : (
      <Clock size={16} className="animate-spin text-warning" />
    )

  const elapsed = formatElapsed(group.startedAt, group.completedAt)

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center gap-3 border-b border-border bg-secondary p-4">
        {turnIcon}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">
            {group.userMessage || (
              <span className="text-muted-foreground">Session activity</span>
            )}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {elapsed}
            {group.status === 'interrupted' && ' · interrupted'}
          </div>
        </div>
      </div>

      <div className="space-y-2 p-4">
        {group.entries.map((entry) => {
          if (entry.type === 'activity') {
            return <ActivityRow key={entry.key} activity={entry.value} />
          }
          if (entry.type === 'message') {
            const msg = entry.value
            // The user's prompt is the card header, and reasoning is carried by
            // its own activity row — rendering either here would duplicate it.
            if (msg.role === 'user' || msg.role === 'reasoning') return null
            return <RichMarkdown key={entry.key} content={msg.text} />
          }
          return null
        })}
      </div>
    </div>
  )
}

/**
 * Distance from the bottom, in pixels, within which the view is treated as
 * "at the live edge" and keeps following new content. Deliberately small: a
 * generous band re-arms follow while the user is reading history and yanks
 * them back down on the next streamed chunk.
 */
const FOLLOW_THRESHOLD_PX = 40

export default function SessionTimeline({ session }: SessionTimelineProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  // Follow starts armed so a freshly opened session lands at the newest turn.
  const followRef = useRef(true)

  const timeline = useMemo(() => buildAgentTimeline(session), [session])
  const groups = useMemo(() => groupByTurn(timeline, session), [timeline, session])

  // Keyed on `session.sequence` rather than `timeline.length`: a streaming turn
  // rewrites one message in place without adding entries, so a length-based
  // effect would stop following exactly while text is arriving.
  useEffect(() => {
    const container = scrollRef.current
    if (!container || !followRef.current) return
    // After paint, so the newly rendered content is included in scrollHeight.
    const frame = requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight
    })
    return () => cancelAnimationFrame(frame)
  }, [session.sequence])

  function handleScroll(event: React.UIEvent<HTMLDivElement>) {
    const target = event.currentTarget
    const distance = target.scrollHeight - target.scrollTop - target.clientHeight
    followRef.current = distance <= FOLLOW_THRESHOLD_PX
  }

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="flex-1 space-y-4 overflow-y-auto p-6"
    >
      {groups.length === 0 && (
        <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
          No activity yet
        </div>
      )}
      {groups.map((group) => (
        <TurnCard key={group.turnId} group={group} />
      ))}
    </div>
  )
}
