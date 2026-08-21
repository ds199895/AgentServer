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

const { applyAgentEvent } = await importTypescript('../src/agent/reducer.ts')

/**
 * The fixture is generated from the real server projection
 * (`app/agent_runtime/service.py::_apply`) by
 * `tests/test_agent_runtime_projection_parity.py`. Folding the same events on
 * the client must land on the same projection, because a gap repair replaces
 * the locally reduced session with a server snapshot — any drift between the
 * two would surface as content changing under the user on reconnect.
 */
const fixture = JSON.parse(
  await readFile(new URL('../../tests/fixtures/agent_projection_parity.json', import.meta.url), 'utf8'),
)

test('client reduction of a full turn matches the server projection exactly', () => {
  const expected = fixture.expected
  // Seed the pre-event state: `session.created` (sequence 1) only sets `starting`.
  let session = {
    ...expected,
    state: 'starting',
    sequence: 1,
    active_turn_id: null,
    last_error: null,
    resume_cursor: null,
    messages: [],
    activities: [],
    requests: [],
    turns: [],
  }
  for (const value of fixture.events) session = applyAgentEvent(session, value)

  for (const field of ['state', 'sequence', 'active_turn_id', 'last_error', 'resume_cursor', 'messages', 'activities', 'requests', 'turns']) {
    assert.deepEqual(
      JSON.parse(JSON.stringify(session[field] ?? null)),
      JSON.parse(JSON.stringify(expected[field] ?? null)),
      `client and server disagree on "${field}"`,
    )
  }
})

test('the fixture exercises the event types the timeline depends on', () => {
  const types = new Set(fixture.events.map((value) => value.type))
  for (const required of [
    'session.ready', 'turn.queued', 'turn.started', 'turn.completed',
    'activity.started', 'activity.updated', 'activity.completed',
    'message.delta', 'message.created', 'plan.updated',
    'request.created', 'request.resolved',
  ]) {
    assert.ok(types.has(required), `fixture is missing ${required}`)
  }
})
