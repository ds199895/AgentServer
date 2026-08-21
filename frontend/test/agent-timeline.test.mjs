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

const { buildAgentTimeline } = await importTypescript('../src/agent/timeline.ts')

test('agent timeline interleaves messages, reasoning, tools, approvals, and final output', () => {
  const session = {
    messages: [
      { id: 'answer', created_at: 6, sequence: 60 },
      { id: 'user', created_at: 1, sequence: 10 },
      { id: 'reasoning', created_at: 2, sequence: 20 },
    ],
    activities: [{ id: 'tool', created_at: 3, sequence: 30, status: 'succeeded' }],
    requests: [{ id: 'approval', created_at: 4, sequence: 40, status: 'resolved' }],
  }
  assert.deepEqual(
    buildAgentTimeline(session).map((item) => item.key),
    ['message:user', 'message:reasoning', 'activity:tool', 'request:approval', 'message:answer'],
  )
})

test('activity updates keep the original timeline position', () => {
  const activity = { id: 'tool', created_at: 2, sequence: 20, status: 'running', output: '' }
  const session = {
    messages: [
      { id: 'user', created_at: 1, sequence: 10 },
      { id: 'answer', created_at: 3, sequence: 30 },
    ],
    activities: [activity],
    requests: [],
  }
  const before = buildAgentTimeline(session).map((item) => item.key)
  activity.status = 'succeeded'
  activity.output = 'complete output'
  assert.deepEqual(buildAgentTimeline(session).map((item) => item.key), before)
})

test('legacy timeline entries without sequence use their event timestamps', () => {
  const session = {
    messages: [{ id: 'answer', created_at: 3 }],
    activities: [{ id: 'tool', created_at: 2 }],
    requests: [{ id: 'approval', created_at: 1 }],
  }
  assert.deepEqual(
    buildAgentTimeline(session).map((item) => item.key),
    ['request:approval', 'activity:tool', 'message:answer'],
  )
})

test('session timeline groups the merged timeline into turns with expandable tool rows', async () => {
  const timeline = await readFile(new URL('../src/sessions/SessionTimeline.tsx', import.meta.url), 'utf8')
  assert.match(timeline, /buildAgentTimeline\(session\)/)
  // Rows carry both sides of a tool call, and collapse by default.
  assert.match(timeline, /inputText/)
  assert.match(timeline, /outputText/)
  assert.match(timeline, /groupByTurn/)
  // The timeline must read the merged projection, not re-walk raw collections.
  assert.doesNotMatch(timeline, /session\.messages\.map/)
  assert.doesNotMatch(timeline, /session\.activities\.map/)
})
