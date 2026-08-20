import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/pixel/bubbles.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const {
  BUBBLE_FADE,
  BUBBLE_POP,
  BUBBLE_TTL,
  BUBBLE_TTL_OUTCOME,
  BubbleTracker,
  bubbleAlpha,
  bubbleContent,
  bubbleKey,
  bubblePinned,
  bubblePop,
} = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

function state(overrides = {}) {
  return { lifecycle: 'running', activity: 'thinking', waitReason: null, stale: false, ...overrides }
}

test('first sight shows a meaningful active state once', () => {
  const tracker = new BubbleTracker()
  const bubble = tracker.track('s1', state(), 10)
  assert.equal(bubble.text, '思考中…')
  // Same state on the next frame keeps the same timed bubble.
  assert.equal(tracker.track('s1', state(), 11), bubble)
})

test('an activity change pops a bubble with the activity text', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state({ activity: 'thinking' }), 10)
  const bubble = tracker.track('s1', state({ activity: 'coding' }), 12)
  assert.ok(bubble)
  assert.equal(bubble.text, '写代码…')
  assert.equal(bubble.tone, 'work')
  assert.equal(bubble.shownAt, 12)
  // Stable state keeps returning the same bubble for the draw loop.
  assert.equal(tracker.track('s1', state({ activity: 'coding' }), 13), bubble)
})

test('a new execution event pops even when the projected activity is unchanged', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state({ activity: 'tooling', eventKey: '10', eventText: '调用 shell', eventTone: 'work' }), 10)
  const bubble = tracker.track('s1', state({ activity: 'tooling', eventKey: '11', eventText: '调用 apply_patch', eventTone: 'work' }), 11)
  assert.equal(bubble.text, '调用 apply_patch')
  assert.equal(bubble.eventKey, '11')
})

test('an old event does not mask a later state-only transition', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state({ eventKey: '10', eventText: '调用 shell', eventTone: 'work' }), 10)
  tracker.track('s1', state({ eventKey: '11', eventText: 'shell 完成', eventTone: 'ok' }), 11)
  const stale = tracker.track('s1', state({ eventKey: '11', eventText: 'shell 完成', eventTone: 'ok', stale: true }), 20)
  assert.equal(stale.text, '状态过期…')
})

test('transient bubbles fade out after their TTL', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state({ activity: 'thinking' }), 0)
  const bubble = tracker.track('s1', state({ activity: 'tooling' }), 10)
  const tooling = state({ activity: 'tooling' })
  assert.equal(bubbleAlpha(bubble, tooling, 10), 1)
  assert.ok(Math.abs(bubbleAlpha(bubble, tooling, 10 + BUBBLE_TTL - BUBBLE_FADE / 2) - 0.5) < 1e-9)
  assert.equal(bubbleAlpha(bubble, tooling, 10 + BUBBLE_TTL), 0)
})

test('a waiting run keeps its bubble pinned until the wait resolves', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state(), 0)
  const waiting = state({ activity: 'waiting', waitReason: 'approval' })
  const bubble = tracker.track('s1', waiting, 5)
  assert.equal(bubble.text, '等待批准')
  assert.equal(bubble.tone, 'wait')
  assert.equal(bubblePinned(waiting), true)
  // Far beyond the transient TTL the bubble is still fully visible.
  assert.equal(bubbleAlpha(bubble, waiting, 500), 1)
  // When the wait resolves, a fresh bubble pops for the new activity.
  const next = tracker.track('s1', state({ activity: 'thinking' }), 600)
  assert.equal(next.text, '思考中…')
  assert.equal(next.shownAt, 600)
})

test('terminal outcomes pop with a longer TTL', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state(), 0)
  const succeeded = state({ lifecycle: 'succeeded', activity: null })
  const bubble = tracker.track('s1', succeeded, 10)
  assert.equal(bubble.text, '已完成')
  assert.equal(bubble.tone, 'ok')
  // Past the transient TTL an outcome bubble is still fully visible…
  assert.equal(bubbleAlpha(bubble, succeeded, 10 + BUBBLE_TTL), 1)
  // …and only expires at the longer outcome TTL.
  assert.equal(bubbleAlpha(bubble, succeeded, 10 + BUBBLE_TTL_OUTCOME), 0)
})

test('terminal outcomes win over stale activity evidence and never pin', () => {
  const terminal = state({ lifecycle: 'failed', activity: 'waiting', waitReason: 'approval', stale: true })
  assert.deepEqual(bubbleContent(terminal), { text: '出错了', tone: 'bad' })
  assert.equal(bubblePinned(terminal), false)
})

test('failed and lost runs pop a bad-tone bubble', () => {
  assert.deepEqual(bubbleContent(state({ lifecycle: 'failed', activity: null })), { text: '出错了', tone: 'bad' })
  assert.deepEqual(bubbleContent(state({ lifecycle: 'lost', activity: null })), { text: '连接丢失', tone: 'bad' })
  assert.deepEqual(bubbleContent(state({ lifecycle: 'cancelled', activity: null })), { text: '已取消', tone: 'mute' })
})

test('stale evidence pops a muted expiry bubble', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state(), 0)
  const bubble = tracker.track('s1', state({ stale: true }), 3)
  assert.equal(bubble.text, '状态过期…')
  assert.equal(bubble.tone, 'mute')
})

test('a change to a contentless state clears the bubble instead of popping', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state(), 0)
  assert.equal(tracker.track('s1', state({ activity: null, lifecycle: null }), 4), null)
  assert.equal(bubbleContent(state({ activity: null, lifecycle: null })), null)
  assert.equal(bubbleContent(state({ activity: 'idle' })), null)
  assert.equal(bubbleContent(state({ activity: 'unknown' })), null)
})

test('prune forgets removed sessions so re-adding is a fresh baseline', () => {
  const tracker = new BubbleTracker()
  tracker.track('s1', state(), 0)
  tracker.prune(new Set(['other']))
  assert.equal(tracker.track('s1', state({ activity: 'coding' }), 5).text, '写代码…')
})

test('bubble keys cover lifecycle, activity, wait reason and staleness', () => {
  assert.notEqual(bubbleKey(state()), bubbleKey(state({ waitReason: 'approval', activity: 'waiting' })))
  assert.notEqual(bubbleKey(state()), bubbleKey(state({ stale: true })))
  assert.notEqual(bubbleKey(state()), bubbleKey(state({ lifecycle: 'failed' })))
  assert.equal(bubbleKey(state()), bubbleKey(state()))
})

test('pop-in progress ramps over BUBBLE_POP and is instant with reduced motion', () => {
  const bubble = { key: 'k', text: '思考中…', tone: 'think', shownAt: 10 }
  assert.equal(bubblePop(bubble, 10, false), 0)
  assert.ok(Math.abs(bubblePop(bubble, 10 + BUBBLE_POP, false) - 1) < 1e-9)
  assert.equal(bubblePop(bubble, 10, true), 1)
})
