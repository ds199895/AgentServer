import type { AgentActivity, AgentMessage, AgentRequest, AgentSession } from './types'

export type AgentTimelineItem =
  | { type: 'message'; key: string; sequence: number; createdAt: number; value: AgentMessage }
  | { type: 'activity'; key: string; sequence: number; createdAt: number; value: AgentActivity }
  | { type: 'request'; key: string; sequence: number; createdAt: number; value: AgentRequest }

function position(value: { sequence?: number; created_at: number }) {
  const sequence = Number(value.sequence)
  return {
    sequence: Number.isFinite(sequence) && sequence > 0 ? sequence : 0,
    createdAt: Number(value.created_at) || 0,
  }
}

export function buildAgentTimeline(session: Pick<AgentSession, 'messages' | 'activities' | 'requests'>): AgentTimelineItem[] {
  const values: AgentTimelineItem[] = [
    ...session.messages.map((value) => ({ type: 'message' as const, key: `message:${value.id}`, value, ...position(value) })),
    ...session.activities.map((value) => ({ type: 'activity' as const, key: `activity:${value.id}`, value, ...position(value) })),
    ...session.requests.map((value) => ({ type: 'request' as const, key: `request:${value.id}`, value, ...position(value) })),
  ]
  return values.sort((left, right) => {
    if (left.sequence && right.sequence && left.sequence !== right.sequence) return left.sequence - right.sequence
    if (left.createdAt !== right.createdAt) return left.createdAt - right.createdAt
    if (left.sequence !== right.sequence) return left.sequence - right.sequence
    return left.key.localeCompare(right.key)
  })
}
