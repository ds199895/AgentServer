import type { ExecutionEvent } from '@/execution-state'

export type ExecutionEventsPage = {
  events: ExecutionEvent[]
  after_sequence: number
  next_sequence: number
  has_more: boolean
  as_of_sequence: number | null
  resync_required: boolean
}

type PageOptions = {
  afterSequence?: number
  limit?: number
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function sequence(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : null
}

function eventSequence(event: ExecutionEvent): number {
  return sequence(event.global_sequence) ?? 0
}

/** Event envelopes are immutable. Replacing a duplicate ID with the newest
 * copy tolerates rolling deployments that add optional response fields while
 * global_sequence keeps the rendered order deterministic. */
export function mergeExecutionEvents(
  current: readonly ExecutionEvent[],
  incoming: readonly ExecutionEvent[],
): ExecutionEvent[] {
  const events = new Map<string, ExecutionEvent>()
  for (const event of [...current, ...incoming]) {
    const key = typeof event.event_id === 'string' && event.event_id
      ? `id:${event.event_id}`
      : `sequence:${eventSequence(event)}`
    events.set(key, event)
  }
  return [...events.values()].sort((left, right) => {
    const bySequence = eventSequence(left) - eventSequence(right)
    if (bySequence) return bySequence
    return String(left.event_id ?? '').localeCompare(String(right.event_id ?? ''))
  })
}

export function normalizeExecutionEventsPage(
  value: unknown,
  options: PageOptions = {},
): ExecutionEventsPage {
  const requestedAfter = Math.max(0, Math.floor(options.afterSequence ?? 0))
  const requestedLimit = Math.max(1, Math.floor(options.limit ?? 200))
  const source = Array.isArray(value) ? null : record(value)
  const rawEvents = Array.isArray(value)
    ? value
    : Array.isArray(source?.events) ? source.events : []
  const events = rawEvents.filter((item): item is ExecutionEvent => record(item) !== null)
  const afterSequence = sequence(source?.after_sequence) ?? requestedAfter
  const lastSequence = events.length ? eventSequence(events[events.length - 1]) : afterSequence
  const nextSequence = Math.max(
    afterSequence,
    sequence(source?.next_sequence) ?? sequence(source?.cursor) ?? lastSequence,
  )
  return {
    events,
    after_sequence: afterSequence,
    next_sequence: nextSequence,
    has_more: typeof source?.has_more === 'boolean'
      ? source.has_more
      : events.length >= requestedLimit,
    as_of_sequence: sequence(source?.as_of_sequence),
    resync_required: source?.resync_required === true,
  }
}
