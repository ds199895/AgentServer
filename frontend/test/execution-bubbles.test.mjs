import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/pixel/execution-bubbles.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const { eventBubbleForTerminal, executionEventBubble } = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

function event(type, payload, overrides = {}) {
  return {
    type,
    event_id: `event-${overrides.global_sequence || 5}`,
    global_sequence: overrides.global_sequence || 5,
    scope: { terminal_id: 'terminal-1', ...overrides.scope },
    payload,
  }
}

test('tool spans expose the tool kind and outcome', () => {
  assert.deepEqual(executionEventBubble(event('span.started', { name: 'shell' })), {
    key: '5:span.started', text: '调用 shell', tone: 'work',
  })
  assert.deepEqual(executionEventBubble(event('span.ended', { name: 'shell', outcome: 'failed' })), {
    key: '5:span.ended', text: 'shell 失败', tone: 'bad',
  })
})

test('summaries are compacted into a safe single-line milestone', () => {
  const bubble = executionEventBubble(event('run.activity.changed', {
    activity: 'testing',
    summary: '  checking\nimportant integration behavior that is deliberately long  ',
  }))
  assert.equal(bubble.text, 'checking important in…')
  assert.equal(bubble.tone, 'mute')
})

test('events route through terminal, run or agent scope without leaking across rooms', () => {
  const snapshot = {
    runs: [{ run_id: 'run-1', terminal_id: 'terminal-1' }],
    agents: [{ agent_instance_id: 'agent-2', terminal_id: 'terminal-2' }],
    recent_events: [
      event('span.started', { name: 'shell' }, { scope: { terminal_id: null, run_id: 'run-1' } }),
      event('span.started', { name: 'apply_patch' }, {
        global_sequence: 6,
        scope: { terminal_id: null, agent_instance_id: 'agent-2' },
      }),
    ],
  }
  assert.equal(eventBubbleForTerminal(snapshot, 'terminal-1').text, '调用 shell')
  assert.equal(eventBubbleForTerminal(snapshot, 'terminal-2').text, '调用 apply_patch')
  assert.equal(eventBubbleForTerminal(snapshot, 'terminal-3'), null)
})

test('an adjacent post-tool activity event does not hide the tool result', () => {
  const producer = { id: 'agent-1', epoch: 'epoch-1', seq: 8 }
  const snapshot = {
    runs: [{ run_id: 'run-1', terminal_id: 'terminal-1' }],
    agents: [],
    recent_events: [
      { ...event('span.ended', { name: 'shell', outcome: 'succeeded' }), producer },
      {
        ...event('run.activity.changed', { activity: 'thinking' }, { global_sequence: 6 }),
        producer: { ...producer, seq: 9 },
      },
    ],
  }
  assert.equal(eventBubbleForTerminal(snapshot, 'terminal-1').text, 'shell 完成')
})
