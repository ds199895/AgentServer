import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/execution-events.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const timeline = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

function event(eventId, globalSequence) {
  return { event_id: eventId, global_sequence: globalSequence }
}

test('new timeline page contract preserves pagination metadata', () => {
  const page = timeline.normalizeExecutionEventsPage({
    events: [event('event-4', 4), event('event-5', 5)],
    after_sequence: 3,
    next_sequence: 5,
    has_more: true,
    as_of_sequence: 12,
    resync_required: false,
  }, { afterSequence: 3, limit: 2 })

  assert.deepEqual(page.events.map((item) => item.event_id), ['event-4', 'event-5'])
  assert.equal(page.after_sequence, 3)
  assert.equal(page.next_sequence, 5)
  assert.equal(page.has_more, true)
  assert.equal(page.as_of_sequence, 12)
  assert.equal(page.resync_required, false)
})

test('legacy list and cursor pages remain pageable without silent truncation', () => {
  const listPage = timeline.normalizeExecutionEventsPage(
    [event('event-1', 1), event('event-2', 2)],
    { afterSequence: 0, limit: 2 },
  )
  assert.equal(listPage.next_sequence, 2)
  assert.equal(listPage.has_more, true)

  const cursorPage = timeline.normalizeExecutionEventsPage({
    events: [event('event-3', 3)],
    cursor: 3,
  }, { afterSequence: 2, limit: 2 })
  assert.equal(cursorPage.next_sequence, 3)
  assert.equal(cursorPage.has_more, false)
})

test('timeline append deduplicates immutable events and orders by global sequence', () => {
  const merged = timeline.mergeExecutionEvents(
    [event('event-2', 2), event('event-4', 4)],
    [event('event-1', 1), event('event-4', 4), event('event-3', 3)],
  )
  assert.deepEqual(
    merged.map((item) => `${item.event_id}:${item.global_sequence}`),
    ['event-1:1', 'event-2:2', 'event-3:3', 'event-4:4'],
  )
})
