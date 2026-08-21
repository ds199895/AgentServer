import { useMemo, useRef, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { ChevronDown, ChevronRight, Brain, Terminal, CheckCircle, XCircle, Clock } from 'lucide-react'
import { buildAgentTimeline, type AgentTimelineItem } from '../agent/timeline'
import type { AgentSession, AgentActivity, AgentMessage } from '../agent/types'

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

function groupByTurn(entries: AgentTimelineItem[], session: AgentSession): TurnGroup[] {
  const groups: TurnGroup[] = []
  const turnMap = new Map<string, TurnGroup>()

  for (const entry of entries) {
    const turnId = entry.value.turn_id
    if (!turnId) continue

    if (!turnMap.has(turnId)) {
      const turn = session.turns.find((t) => t.id === turnId)
      const userMsg = session.messages.find((m) => m.role === 'user' && m.turn_id === turnId)

      const group: TurnGroup = {
        turnId,
        userMessage: userMsg?.text || turn?.input || '',
        entries: [],
        startedAt: turn?.created_at || entry.createdAt,
        completedAt: turn?.completed_at || null,
        status: turn?.state === 'completed' ? 'completed'
               : turn?.state === 'failed' ? 'failed'
               : turn?.state === 'interrupted' ? 'interrupted'
               : 'running',
      }
      turnMap.set(turnId, group)
      groups.push(group)
    }

    turnMap.get(turnId)!.entries.push(entry)
  }

  return groups
}

function formatElapsed(startMs: number, endMs: number | null): string {
  const elapsed = (endMs || Date.now()) - startMs * 1000
  if (elapsed < 1000) return `${Math.round(elapsed)}ms`
  if (elapsed < 60000) return `${(elapsed / 1000).toFixed(1)}s`
  return `${Math.floor(elapsed / 60000)}m ${Math.floor((elapsed % 60000) / 1000)}s`
}

function RichMarkdown({ content }: { content: string }) {
  return (
    <div className="prose prose-sm max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight as any]}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

function ActivityRow({ activity }: { activity: AgentActivity }) {
  const icon = activity.status === 'succeeded' || activity.status === 'completed' ? (
    <CheckCircle size={16} className="text-green-600" />
  ) : activity.status === 'failed' ? (
    <XCircle size={16} className="text-red-600" />
  ) : activity.kind === 'command' ? (
    <Terminal size={16} className="text-blue-600" />
  ) : (
    <Brain size={16} className="text-purple-600" />
  )

  const outputText = activity.output
    ? (typeof activity.output === 'string' ? activity.output : JSON.stringify(activity.output, null, 2))
    : null

  return (
    <div className="flex items-start gap-3 p-3 bg-gray-50 rounded-lg border border-gray-200">
      <div className="mt-0.5">{icon}</div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-gray-900">{activity.title}</div>
        {activity.detail && (
          <div className="text-xs text-gray-600 mt-1 font-mono">{String(activity.detail)}</div>
        )}
        {outputText && (
          <pre className="text-xs text-gray-800 mt-2 p-2 bg-white rounded border border-gray-200 overflow-x-auto">
            {outputText}
          </pre>
        )}
      </div>
    </div>
  )
}

function TurnCard({ group, isLast }: { group: TurnGroup; isLast: boolean }) {
  const statusIcon = group.status === 'completed' ? (
    <CheckCircle size={16} className="text-green-600" />
  ) : group.status === 'failed' ? (
    <XCircle size={16} className="text-red-600" />
  ) : (
    <Clock size={16} className="text-blue-600 animate-spin" />
  )

  const elapsed = formatElapsed(group.startedAt, group.completedAt)

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
      <div className="flex items-center gap-3 p-4 bg-gray-50 border-b border-gray-200">
        {statusIcon}
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-gray-900 truncate">{group.userMessage}</div>
          <div className="text-xs text-gray-500 mt-1">{elapsed}</div>
        </div>
      </div>

      <div className="p-4 space-y-3">
        {group.entries.map((entry) => {
          if (entry.type === 'activity') {
            return <ActivityRow key={entry.key} activity={entry.value} />
          }
          if (entry.type === 'message') {
            const msg = entry.value
            if (msg.role === 'user') return null
            if (msg.role === 'reasoning') return null // Skip reasoning duplicates

            return (
              <div key={entry.key} className="text-sm text-gray-800">
                <RichMarkdown content={msg.text} />
              </div>
            )
          }
          return null
        })}
      </div>
    </div>
  )
}

export default function SessionTimeline({ session }: SessionTimelineProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const prevLengthRef = useRef(0)

  const timeline = useMemo(() => buildAgentTimeline(session), [session])
  const groups = useMemo(() => groupByTurn(timeline, session), [timeline, session])

  useEffect(() => {
    if (timeline.length > prevLengthRef.current && scrollRef.current) {
      const container = scrollRef.current
      const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100

      if (isNearBottom || prevLengthRef.current === 0) {
        requestAnimationFrame(() => {
          container.scrollTop = container.scrollHeight
        })
      }
    }
    prevLengthRef.current = timeline.length
  }, [timeline.length])

  return (
    <div ref={scrollRef} className="flex-1 overflow-y-auto p-6 space-y-4">
      {groups.length === 0 && (
        <div className="flex items-center justify-center h-full text-gray-500 text-sm">
          No activity yet
        </div>
      )}
      {groups.map((group, index) => (
        <TurnCard key={group.turnId} group={group} isLast={index === groups.length - 1} />
      ))}
    </div>
  )
}
