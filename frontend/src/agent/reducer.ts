import type { AgentActivity, AgentEvent, AgentMessage, AgentRequest, AgentSession, AgentTurn } from './types'

/**
 * Client-side projection of the server event log.
 *
 * The server applies the same transitions in `app/agent_runtime/service.py::_apply`;
 * both sides must stay in step so a locally reduced session is byte-equivalent to
 * the snapshot the server would return at the same sequence. Reducing locally is
 * what keeps a streaming turn cheap: a `message.delta` rewrites one message object
 * instead of refetching the whole transcript.
 */

const TERMINAL_SESSION_STATES: AgentSession['state'][] = [
  'starting', 'ready', 'running', 'waiting', 'disconnected', 'stopping', 'stopped', 'failed',
]

function text(value: unknown): string {
  return value === undefined || value === null ? '' : String(value)
}

/** Replace one element by index, leaving every other element referentially stable. */
function replaceAt<T>(values: T[], index: number, value: T): T[] {
  const next = values.slice()
  next[index] = value
  return next
}

function findLastIndex<T>(values: T[], predicate: (value: T) => boolean): number {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (predicate(values[index])) return index
  }
  return -1
}

/**
 * True when `value` is not the next event this session expects. The socket
 * replays from a cursor, so an out-of-order arrival means the local projection
 * can no longer be trusted and the caller must refetch the snapshot.
 */
export function hasSequenceGap(session: AgentSession, value: AgentEvent): boolean {
  return value.sequence > session.sequence + 1
}

/** True when the event was already folded into this session. */
export function isAlreadyApplied(session: AgentSession, value: AgentEvent): boolean {
  return value.sequence <= session.sequence
}

function applyTurnState(
  session: AgentSession,
  value: AgentEvent,
  state: AgentTurn['state'],
  error?: string,
): AgentSession {
  const turnId = text(value.payload.turn_id)
  const index = findLastIndex(session.turns, (turn) => turn.id === turnId)
  const turns = index < 0 ? session.turns : replaceAt(session.turns, index, {
    ...session.turns[index],
    state,
    completed_at: value.occurred_at,
    ...(error === undefined ? {} : { error }),
  })
  return { ...session, state: 'ready', active_turn_id: null, turns }
}

function applyMessage(session: AgentSession, value: AgentEvent): AgentSession {
  const payload = value.payload
  const id = text(payload.message_id) || value.event_id
  const fragment = text(payload.text)
  const streaming = value.type === 'message.delta'
  const index = session.messages.findIndex((message) => message.id === id)
  if (index < 0) {
    const message: AgentMessage = {
      id,
      session_id: session.id,
      role: (text(payload.role) || 'assistant') as AgentMessage['role'],
      text: fragment,
      turn_id: (payload.turn_id as string | null | undefined) ?? null,
      item_id: (payload.item_id as string | null | undefined) ?? null,
      created_at: value.occurred_at,
      streaming,
      sequence: value.sequence,
    }
    return { ...session, messages: [...session.messages, message] }
  }
  const current = session.messages[index]
  const message: AgentMessage = streaming
    ? { ...current, text: current.text + fragment, streaming: true }
    // A completed message carries the authoritative final text, but providers
    // may send it empty when every character already arrived as deltas.
    : { ...current, text: fragment || current.text, streaming: false }
  return { ...session, messages: replaceAt(session.messages, index, message) }
}

function applyActivityStarted(session: AgentSession, value: AgentEvent): AgentSession {
  const payload = value.payload
  const id = text(payload.activity_id) || value.event_id
  if (findLastIndex(session.activities, (activity) => activity.id === id) >= 0) return session
  const activity: AgentActivity = {
    id,
    session_id: session.id,
    kind: text(payload.kind) || 'status',
    title: text(payload.title) || 'Working',
    status: text(payload.status) || 'running',
    detail: text(payload.detail),
    input: payload.input ?? null,
    output: payload.output ?? null,
    turn_id: (payload.turn_id as string | null | undefined) ?? null,
    item_id: (payload.item_id as string | null | undefined) ?? null,
    created_at: value.occurred_at,
    updated_at: value.occurred_at,
    collapsed: payload.collapsed === undefined ? true : Boolean(payload.collapsed),
    sequence: value.sequence,
  }
  return { ...session, activities: [...session.activities, activity] }
}

function applyActivityProgress(session: AgentSession, value: AgentEvent): AgentSession {
  const payload = value.payload
  const completed = value.type === 'activity.completed'
  const id = text(payload.activity_id)
  const index = findLastIndex(session.activities, (activity) => activity.id === id)
  let activities = session.activities
  if (index >= 0) {
    const current = session.activities[index]
    const activity: AgentActivity = { ...current, updated_at: value.occurred_at }
    activity.status = text(payload.status) || (completed ? 'completed' : current.status)
    if (payload.title) activity.title = text(payload.title)
    if (payload.detail) activity.detail = text(payload.detail)
    if (payload.detail_delta) activity.detail = `${activity.detail || ''}${text(payload.detail_delta)}`
    if ('input' in payload && payload.input !== null && payload.input !== undefined) activity.input = payload.input
    if ('output' in payload && payload.output !== null && payload.output !== undefined) activity.output = payload.output
    if (payload.output_delta) activity.output = `${text(current.output ?? '')}${text(payload.output_delta)}`
    activities = replaceAt(session.activities, index, activity)
  } else if (id) {
    activities = [...session.activities, {
      id,
      session_id: session.id,
      kind: text(payload.kind) || 'output',
      title: text(payload.title) || 'Tool output',
      status: text(payload.status) || (completed ? 'completed' : 'running'),
      detail: text(payload.detail) || text(payload.detail_delta),
      input: payload.input ?? null,
      output: payload.output !== null && payload.output !== undefined ? payload.output : (payload.output_delta ?? null),
      turn_id: (payload.turn_id as string | null | undefined) ?? null,
      item_id: (payload.item_id as string | null | undefined) ?? null,
      created_at: value.occurred_at,
      updated_at: value.occurred_at,
      collapsed: true,
      sequence: value.sequence,
    }]
  }
  let messages = session.messages
  if (completed && payload.item_id) {
    // Reasoning arrives as a message but its lifecycle is carried by the
    // matching activity, so the item completing is what ends the stream.
    const index = findLastIndex(messages, (message) => message.role === 'reasoning' && message.item_id === payload.item_id)
    if (index >= 0 && messages[index].streaming) {
      messages = replaceAt(messages, index, { ...messages[index], streaming: false })
    }
  }
  return { ...session, activities, messages }
}

function applyEventBody(session: AgentSession, value: AgentEvent): AgentSession {
  const payload = value.payload
  switch (value.type) {
    case 'session.created':
      return { ...session, state: 'starting' }
    case 'session.ready': {
      const providerSessionId = payload.provider_session_id || payload.provider_thread_id
      return {
        ...session,
        state: 'ready',
        resume_cursor: providerSessionId ? { thread_id: text(providerSessionId) } : session.resume_cursor,
      }
    }
    case 'session.stopped':
      return { ...session, state: 'stopped' }
    case 'session.failed':
      return { ...session, state: 'failed', last_error: text(payload.error) || 'provider failed' }
    case 'session.state.changed': {
      const state = text(payload.state) as AgentSession['state']
      return TERMINAL_SESSION_STATES.includes(state) ? { ...session, state } : session
    }
    case 'turn.queued': {
      const turnId = text(payload.turn_id) || value.event_id
      if (session.turns.some((turn) => turn.id === turnId)) return session
      const input = text(payload.input)
      return {
        ...session,
        turns: [...session.turns, {
          id: turnId, session_id: session.id, input, state: 'queued', created_at: value.occurred_at, completed_at: null, error: null,
        }],
        messages: [...session.messages, {
          id: `user-${turnId}`, session_id: session.id, role: 'user', text: input,
          turn_id: turnId, item_id: null, created_at: value.occurred_at, streaming: false, sequence: value.sequence,
        }],
      }
    }
    case 'turn.started': {
      const turnId = text(payload.turn_id)
      const index = findLastIndex(session.turns, (turn) => turn.id === turnId)
      const turns = index >= 0
        ? replaceAt(session.turns, index, { ...session.turns[index], state: 'running' as const })
        : [...session.turns, {
            id: turnId, session_id: session.id, input: text(payload.input),
            state: 'running' as const, created_at: value.occurred_at, completed_at: null, error: null,
          }]
      return { ...session, state: 'running', active_turn_id: turnId, turns }
    }
    case 'turn.completed':
      return applyTurnState(session, value, 'completed')
    case 'turn.interrupted':
      return applyTurnState(session, value, 'interrupted')
    case 'turn.failed':
      return applyTurnState(session, value, 'failed', text(payload.error) || 'provider turn failed')
    case 'message.created':
    case 'message.delta':
      return applyMessage(session, value)
    case 'activity.started':
      return applyActivityStarted(session, value)
    case 'activity.updated':
    case 'activity.completed':
      return applyActivityProgress(session, value)
    case 'request.created': {
      const request: AgentRequest = {
        id: text(payload.request_id) || value.event_id,
        session_id: session.id,
        kind: (text(payload.kind) || 'user_input') as AgentRequest['kind'],
        title: text(payload.title) || 'Input required',
        detail: text(payload.detail),
        options: Array.isArray(payload.options) ? payload.options as Array<Record<string, unknown>> : [],
        status: 'pending',
        turn_id: (payload.turn_id as string | null | undefined) ?? null,
        created_at: value.occurred_at,
        input: payload.input ?? null,
        response: null,
        resolved_at: null,
        sequence: value.sequence,
      }
      return { ...session, state: 'waiting', requests: [...session.requests, request] }
    }
    case 'request.resolved': {
      const index = findLastIndex(session.requests, (request) => request.id === payload.request_id)
      const requests = index < 0 ? session.requests : replaceAt(session.requests, index, {
        ...session.requests[index],
        status: 'resolved' as const,
        response: 'resolution' in payload ? payload.resolution : payload.response,
        resolved_at: value.occurred_at,
      })
      return { ...session, state: session.active_turn_id ? 'running' : 'ready', requests }
    }
    case 'plan.updated':
      return {
        ...session,
        activities: [...session.activities, {
          id: text(payload.activity_id) || value.event_id,
          session_id: session.id,
          kind: 'plan',
          title: 'Plan updated',
          status: 'completed',
          detail: text(payload.detail),
          input: payload.plan,
          output: null,
          turn_id: (payload.turn_id as string | null | undefined) ?? null,
          item_id: null,
          created_at: value.occurred_at,
          updated_at: value.occurred_at,
          collapsed: true,
          sequence: value.sequence,
        }],
      }
    default:
      return session
  }
}

/**
 * Fold one event into the session projection. Callers must reject gaps with
 * `hasSequenceGap` first; this assumes `value` is the next expected event.
 */
export function applyAgentEvent(session: AgentSession, value: AgentEvent): AgentSession {
  const next = applyEventBody(session, value)
  return { ...next, sequence: value.sequence, updated_at: value.occurred_at }
}
