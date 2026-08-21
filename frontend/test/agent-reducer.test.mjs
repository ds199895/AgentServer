import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { transformWithOxc } from 'vite'

async function importTypescript(relativePath) {
  const sourceUrl = new URL(relativePath, import.meta.url)
  const source = await readFile(sourceUrl, 'utf8')
  const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
  return import(`data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`)
}

const { applyAgentEvent, hasSequenceGap, isAlreadyApplied } = await importTypescript('../src/agent/reducer.ts')

function emptySession(overrides = {}) {
  return {
    id: 'session-1',
    device_id: 'device-a',
    provider: 'codex',
    cwd: '/workspace',
    permission_mode: 'workspace-write',
    model: null,
    state: 'starting',
    session_kind: 'agent',
    created_at: 1,
    updated_at: 1,
    active_turn_id: null,
    last_error: null,
    resume_cursor: null,
    sequence: 0,
    executor_id: '',
    bridge_instance_id: '',
    transport: '',
    device_generation: 0,
    platform: {},
    capabilities: {},
    connector_sequence: 0,
    messages: [],
    activities: [],
    requests: [],
    turns: [],
    ...overrides,
  }
}

let nextSequence = 0
function event(type, payload, occurredAt = 100) {
  nextSequence += 1
  return { sequence: nextSequence, event_id: `event-${nextSequence}`, session_id: 'session-1', type, payload, occurred_at: occurredAt }
}

function reduce(session, events) {
  return events.reduce((current, value) => applyAgentEvent(current, value), session)
}

test('a streaming turn projects into user message, tool activity, and assistant text', () => {
  nextSequence = 0
  const session = reduce(emptySession(), [
    event('session.ready', { provider_thread_id: 'thread-9' }),
    event('turn.queued', { turn_id: 'turn-1', input: 'list the files' }),
    event('turn.started', { turn_id: 'turn-1' }),
    event('activity.started', { activity_id: 'item-1', kind: 'command', title: 'Command', detail: 'ls -la' }),
    event('activity.updated', { activity_id: 'item-1', output_delta: 'a.txt\n' }),
    event('activity.updated', { activity_id: 'item-1', output_delta: 'b.txt\n' }),
    event('activity.completed', { activity_id: 'item-1', status: 'succeeded' }),
    event('message.delta', { message_id: 'msg-1', role: 'assistant', text: 'Two ' }),
    event('message.delta', { message_id: 'msg-1', role: 'assistant', text: 'files.' }),
    event('message.created', { message_id: 'msg-1', role: 'assistant', text: 'Two files.' }),
    event('turn.completed', { turn_id: 'turn-1' }),
  ])

  assert.equal(session.state, 'ready')
  assert.equal(session.active_turn_id, null)
  assert.deepEqual(session.resume_cursor, { thread_id: 'thread-9' })
  assert.equal(session.sequence, 11)

  assert.deepEqual(session.messages.map((message) => [message.role, message.text, message.streaming]), [
    ['user', 'list the files', false],
    ['assistant', 'Two files.', false],
  ])
  assert.equal(session.activities.length, 1)
  assert.equal(session.activities[0].output, 'a.txt\nb.txt\n')
  assert.equal(session.activities[0].status, 'succeeded')
  assert.deepEqual(session.turns.map((turn) => [turn.id, turn.state]), [['turn-1', 'completed']])
})

test('assistant deltas mutate only their own message object', () => {
  nextSequence = 0
  const before = reduce(emptySession(), [
    event('turn.queued', { turn_id: 'turn-1', input: 'hello' }),
    event('message.delta', { message_id: 'msg-1', role: 'assistant', text: 'partial' }),
  ])
  const after = applyAgentEvent(before, event('message.delta', { message_id: 'msg-1', role: 'assistant', text: ' more' }))

  assert.equal(after.messages[1].text, 'partial more')
  // The user message must keep its identity so React can skip re-rendering it.
  assert.equal(after.messages[0], before.messages[0])
  assert.notEqual(after.messages[1], before.messages[1])
})

test('a completed message with empty text keeps the streamed text', () => {
  nextSequence = 0
  const session = reduce(emptySession(), [
    event('message.delta', { message_id: 'msg-1', role: 'assistant', text: 'streamed answer' }),
    event('message.created', { message_id: 'msg-1', role: 'assistant', text: '' }),
  ])
  assert.equal(session.messages[0].text, 'streamed answer')
  assert.equal(session.messages[0].streaming, false)
})

test('reasoning stops streaming when its owning item completes', () => {
  nextSequence = 0
  const session = reduce(emptySession(), [
    event('message.delta', { message_id: 'reasoning-1', role: 'reasoning', text: 'thinking…', item_id: 'item-1' }),
    event('activity.completed', { activity_id: 'item-1', item_id: 'item-1', status: 'succeeded' }),
  ])
  assert.equal(session.messages[0].role, 'reasoning')
  assert.equal(session.messages[0].streaming, false)
})

test('approval requests block the turn and resolve back to running', () => {
  nextSequence = 0
  const waiting = reduce(emptySession(), [
    event('turn.queued', { turn_id: 'turn-1', input: 'remove the file' }),
    event('turn.started', { turn_id: 'turn-1' }),
    event('request.created', { request_id: 'request-1', kind: 'approval', title: 'Run command?', options: [] }),
  ])
  assert.equal(waiting.state, 'waiting')
  assert.equal(waiting.requests[0].status, 'pending')

  const resolved = applyAgentEvent(waiting, event('request.resolved', { request_id: 'request-1', resolution: { decision: 'approve_once' } }))
  assert.equal(resolved.state, 'running')
  assert.equal(resolved.requests[0].status, 'resolved')
  assert.deepEqual(resolved.requests[0].response, { decision: 'approve_once' })
})

test('a failed turn records its error and leaves the session usable', () => {
  nextSequence = 0
  const session = reduce(emptySession(), [
    event('turn.queued', { turn_id: 'turn-1', input: 'break' }),
    event('turn.started', { turn_id: 'turn-1' }),
    event('turn.failed', { turn_id: 'turn-1', error: 'provider exploded' }),
  ])
  assert.equal(session.state, 'ready')
  assert.equal(session.turns[0].state, 'failed')
  assert.equal(session.turns[0].error, 'provider exploded')
})

test('unknown event types advance the cursor without altering the projection', () => {
  nextSequence = 0
  const before = reduce(emptySession(), [event('message.delta', { message_id: 'msg-1', text: 'hi' })])
  const after = applyAgentEvent(before, event('thread.metadata.updated', { title: 'ignored' }))
  assert.deepEqual(after.messages, before.messages)
  assert.equal(after.sequence, 2)
})

test('replayed and gapped events are detectable before they are applied', () => {
  const session = emptySession({ sequence: 5 })
  assert.equal(isAlreadyApplied(session, { sequence: 5 }), true)
  assert.equal(isAlreadyApplied(session, { sequence: 6 }), false)
  assert.equal(hasSequenceGap(session, { sequence: 6 }), false)
  assert.equal(hasSequenceGap(session, { sequence: 7 }), true)
})

test('the session hook reduces events locally instead of refetching per event', async () => {
  const hook = await readFile(new URL('../src/agent/useAgentSession.ts', import.meta.url), 'utf8')
  assert.match(hook, /applyAgentEvent\(current, event\)/)
  assert.match(hook, /hasSequenceGap\(current, event\)/)
  // A per-event snapshot refetch is what P0 removed; only gap repair may refetch.
  assert.doesNotMatch(hook, /onmessage = async/)
  assert.doesNotMatch(hook, /await reload\(\)/)
})
