import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

// The frontend intentionally has no runtime test framework. Transpile this
// dependency-free state module in memory so the contracts can run on Node 20
// without adding a second bundler or checking generated JavaScript into git.
const sourceUrl = new URL('../src/terminal-layout.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })

const layout = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

const {
  activateSession,
  closeLeaf,
  detachSession,
  findLeaf,
  leafOfSession,
  listLeaves,
  listSessionIds,
  moveSession,
  parseLayout,
  pinSession,
  previewSession,
  reconcile,
  removeSession,
  serializeLayout,
  splitLeaf,
} = layout

function leaf(id, tabs = [], activeTab = tabs[0] ?? null, previewTab = null) {
  return { type: 'leaf', id, tabs, activeTab, previewTab }
}

function split(id, first, second, direction = 'row', ratio = 0.5) {
  return { type: 'split', id, direction, ratio, children: [first, second] }
}

test('each leaf switches its activeTab independently without changing preview state', () => {
  const left = leaf('left', ['left-a', 'left-b'], 'left-a')
  const right = leaf('right', ['right-a', 'right-preview'], 'right-a', 'right-preview')
  const root = split('root', left, right)

  const leftResult = activateSession(root, 'left-b')
  assert.equal(leftResult.leafId, 'left')
  assert.equal(findLeaf(leftResult.root, 'left').activeTab, 'left-b')
  assert.equal(findLeaf(leftResult.root, 'right').activeTab, 'right-a')
  assert.equal(findLeaf(leftResult.root, 'right').previewTab, 'right-preview')
  assert.strictEqual(findLeaf(leftResult.root, 'right'), right)

  const rightResult = activateSession(leftResult.root, 'right-preview')
  assert.equal(rightResult.leafId, 'right')
  assert.equal(findLeaf(rightResult.root, 'left').activeTab, 'left-b')
  assert.equal(findLeaf(rightResult.root, 'right').activeTab, 'right-preview')
  assert.equal(findLeaf(rightResult.root, 'right').previewTab, 'right-preview')
})

test('activateSession reveals assigned sessions and assigns orphan sessions as pinned tabs', () => {
  const left = leaf('left', ['left-a', 'left-b'], 'left-a')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  const reveal = activateSession(root, 'left-b', { leafId: 'right' })
  assert.equal(reveal.leafId, 'left')
  assert.equal(findLeaf(reveal.root, 'left').activeTab, 'left-b')
  assert.deepEqual(findLeaf(reveal.root, 'right').tabs, ['right-a'])
  assert.equal(listSessionIds(reveal.root).filter((id) => id === 'left-b').length, 1)

  const assign = activateSession(reveal.root, 'orphan', { leafId: 'right' })
  assert.equal(assign.leafId, 'right')
  assert.deepEqual(findLeaf(assign.root, 'right').tabs, ['right-a', 'orphan'])
  assert.equal(findLeaf(assign.root, 'right').activeTab, 'orphan')
  assert.equal(findLeaf(assign.root, 'right').previewTab, null)
  assert.equal(findLeaf(assign.root, 'left').activeTab, 'left-b')

  const invalidTarget = activateSession(root, 'fallback', { leafId: 'missing' })
  assert.equal(invalidTarget.leafId, 'left')
  assert.deepEqual(findLeaf(invalidTarget.root, 'left').tabs, ['left-a', 'left-b', 'fallback'])

  const bootstrap = activateSession(null, 'first')
  assert.deepEqual(bootstrap.root.tabs, ['first'])
  assert.equal(bootstrap.root.activeTab, 'first')
  assert.equal(bootstrap.root.previewTab, null)
  assert.equal(bootstrap.leafId, bootstrap.root.id)
})

test('activateSession replaces a transient preview when opening a pinned orphan', () => {
  const left = leaf('left', ['fixed', 'old-preview'], 'old-preview', 'old-preview')
  const right = leaf('right', ['right-fixed'], 'right-fixed')
  const root = split('root', left, right)

  const result = activateSession(root, 'new-pinned', { leafId: 'left' })
  const nextLeft = findLeaf(result.root, 'left')
  assert.equal(result.leafId, 'left')
  assert.deepEqual(nextLeft.tabs, ['fixed', 'new-pinned'])
  assert.equal(nextLeft.activeTab, 'new-pinned')
  assert.equal(nextLeft.previewTab, null)
  assert.equal(leafOfSession(result.root, 'old-preview'), null)
  assert.strictEqual(findLeaf(result.root, 'right'), right)
})

test('previewSession bootstraps a preview and replaces at most one preview in its target pane', () => {
  const bootstrap = previewSession(null, 'first-preview')
  assert.deepEqual(bootstrap.root.tabs, ['first-preview'])
  assert.equal(bootstrap.root.activeTab, 'first-preview')
  assert.equal(bootstrap.root.previewTab, 'first-preview')

  const left = leaf(
    'left',
    ['fixed-a', 'fixed-b', 'old-preview'],
    'old-preview',
    'old-preview',
  )
  const right = leaf('right', ['right-fixed'], 'right-fixed')
  const root = split('root', left, right)
  const result = previewSession(root, 'new-preview', { leafId: 'left' })
  const nextLeft = findLeaf(result.root, 'left')

  assert.equal(result.leafId, 'left')
  assert.deepEqual(nextLeft.tabs, ['fixed-a', 'fixed-b', 'new-preview'])
  assert.equal(nextLeft.activeTab, 'new-preview')
  assert.equal(nextLeft.previewTab, 'new-preview')
  assert.equal(leafOfSession(result.root, 'old-preview'), null)
  assert.equal(listSessionIds(result.root).filter((id) => id === 'new-preview').length, 1)
  assert.strictEqual(findLeaf(result.root, 'right'), right)

  const repeated = previewSession(result.root, 'new-preview', { leafId: 'left' })
  assert.strictEqual(repeated.root, result.root)
  assert.equal(repeated.leafId, 'left')
})

test('previewSession reveals pinned tabs in their owner and never moves them to the requested pane', () => {
  const left = leaf('left', ['left-pinned', 'left-preview'], 'left-preview', 'left-preview')
  const right = leaf('right', ['right-pinned', 'right-preview'], 'right-preview', 'right-preview')
  const root = split('root', left, right)

  const result = previewSession(root, 'left-pinned', { leafId: 'right' })
  assert.equal(result.leafId, 'left')
  assert.equal(findLeaf(result.root, 'left').activeTab, 'left-pinned')
  assert.equal(findLeaf(result.root, 'left').previewTab, 'left-preview')
  assert.deepEqual(findLeaf(result.root, 'right'), right)
  assert.equal(listSessionIds(result.root).filter((id) => id === 'left-pinned').length, 1)
})

test('previewSession moves a preview across panes while preserving global ownership uniqueness', () => {
  const left = leaf('left', ['left-pinned', 'moving-preview'], 'moving-preview', 'moving-preview')
  const right = leaf('right', ['right-pinned', 'displaced-preview'], 'displaced-preview', 'displaced-preview')
  const root = split('root', left, right)

  const result = previewSession(root, 'moving-preview', { leafId: 'right' })
  const nextLeft = findLeaf(result.root, 'left')
  const nextRight = findLeaf(result.root, 'right')

  assert.equal(result.leafId, 'right')
  assert.deepEqual(nextLeft.tabs, ['left-pinned'])
  assert.equal(nextLeft.activeTab, 'left-pinned')
  assert.equal(nextLeft.previewTab, null)
  assert.deepEqual(nextRight.tabs, ['right-pinned', 'moving-preview'])
  assert.equal(nextRight.activeTab, 'moving-preview')
  assert.equal(nextRight.previewTab, 'moving-preview')
  assert.equal(leafOfSession(result.root, 'displaced-preview'), null)
  assert.equal(listSessionIds(result.root).filter((id) => id === 'moving-preview').length, 1)
})

test('pinSession makes a preview durable and pinned sessions still reveal their owner', () => {
  const left = leaf('left', ['left-fixed', 'left-preview'], 'left-preview', 'left-preview')
  const right = leaf('right', ['right-fixed'], 'right-fixed')
  const root = split('root', left, right)

  const pinned = pinSession(root, 'left-preview', { leafId: 'left' })
  const pinnedLeft = findLeaf(pinned.root, 'left')
  assert.equal(pinned.leafId, 'left')
  assert.deepEqual(pinnedLeft.tabs, ['left-fixed', 'left-preview'])
  assert.equal(pinnedLeft.activeTab, 'left-preview')
  assert.equal(pinnedLeft.previewTab, null)

  const nextPreview = previewSession(pinned.root, 'next-preview', { leafId: 'left' })
  assert.deepEqual(
    findLeaf(nextPreview.root, 'left').tabs,
    ['left-fixed', 'left-preview', 'next-preview'],
  )
  assert.equal(findLeaf(nextPreview.root, 'left').previewTab, 'next-preview')

  const revealPinned = pinSession(nextPreview.root, 'left-fixed', { leafId: 'right' })
  assert.equal(revealPinned.leafId, 'left')
  assert.equal(findLeaf(revealPinned.root, 'left').activeTab, 'left-fixed')
  assert.deepEqual(findLeaf(revealPinned.root, 'right'), right)

  const repeated = pinSession(pinned.root, 'left-preview', { leafId: 'left' })
  assert.strictEqual(repeated.root, pinned.root)

  const bootstrap = pinSession(null, 'first-pinned')
  assert.deepEqual(bootstrap.root.tabs, ['first-pinned'])
  assert.equal(bootstrap.root.previewTab, null)
})

test('pinSession can move a preview across panes and displaces only the target preview', () => {
  const left = leaf('left', ['left-fixed', 'moving-preview'], 'moving-preview', 'moving-preview')
  const right = leaf('right', ['right-fixed', 'right-preview'], 'right-preview', 'right-preview')
  const root = split('root', left, right)

  const result = pinSession(root, 'moving-preview', { leafId: 'right' })
  assert.equal(result.leafId, 'right')
  assert.deepEqual(findLeaf(result.root, 'left').tabs, ['left-fixed'])
  assert.equal(findLeaf(result.root, 'left').previewTab, null)
  assert.deepEqual(findLeaf(result.root, 'right').tabs, ['right-fixed', 'moving-preview'])
  assert.equal(findLeaf(result.root, 'right').activeTab, 'moving-preview')
  assert.equal(findLeaf(result.root, 'right').previewTab, null)
  assert.equal(leafOfSession(result.root, 'right-preview'), null)
  assert.equal(listSessionIds(result.root).filter((id) => id === 'moving-preview').length, 1)
})

test('moveSession moves a pinned tab into an empty pane without collapsing its source', () => {
  const source = leaf('source', ['only-pinned'], 'only-pinned')
  const target = leaf('target')
  const untouched = leaf('untouched', ['stay'], 'stay')
  const root = split('root', split('nested', source, target, 'column'), untouched)

  const result = moveSession(root, 'only-pinned', {
    sourceLeafId: 'source',
    targetLeafId: 'target',
    targetIndex: 0,
  })

  assert.deepEqual(findLeaf(result, 'source'), {
    type: 'leaf',
    id: 'source',
    tabs: [],
    activeTab: null,
    previewTab: null,
  })
  assert.deepEqual(findLeaf(result, 'target'), {
    type: 'leaf',
    id: 'target',
    tabs: ['only-pinned'],
    activeTab: 'only-pinned',
    previewTab: null,
  })
  assert.equal(listLeaves(result).length, 3)
  assert.deepEqual(listLeaves(result).map((item) => item.id), ['source', 'target', 'untouched'])
  assert.equal(listSessionIds(result).filter((id) => id === 'only-pinned').length, 1)
  assert.strictEqual(findLeaf(result, 'untouched'), untouched)
})

test('moveSession keeps pinned mode and preserves an existing target preview', () => {
  const source = leaf('source', ['source-active', 'moving-pinned'], 'source-active')
  const target = leaf(
    'target',
    ['target-fixed', 'target-preview'],
    'target-preview',
    'target-preview',
  )
  const root = split('root', source, target)

  const result = moveSession(root, 'moving-pinned', {
    sourceLeafId: 'source',
    targetLeafId: 'target',
    targetIndex: 1,
  })

  assert.deepEqual(findLeaf(result, 'source').tabs, ['source-active'])
  assert.equal(findLeaf(result, 'source').activeTab, 'source-active')
  assert.equal(findLeaf(result, 'source').previewTab, null)
  assert.deepEqual(
    findLeaf(result, 'target').tabs,
    ['target-fixed', 'moving-pinned', 'target-preview'],
  )
  assert.equal(findLeaf(result, 'target').activeTab, 'moving-pinned')
  assert.equal(findLeaf(result, 'target').previewTab, 'target-preview')
  assert.equal(listSessionIds(result).filter((id) => id === 'moving-pinned').length, 1)
})

test('moveSession moves a sole preview into an empty pane and preserves preview mode', () => {
  const source = leaf('source', ['moving-preview'], 'moving-preview', 'moving-preview')
  const target = leaf('target')
  const root = split('root', source, target)

  const result = moveSession(root, 'moving-preview', {
    sourceLeafId: 'source',
    targetLeafId: 'target',
    targetIndex: 0,
  })

  assert.deepEqual(findLeaf(result, 'source').tabs, [])
  assert.equal(findLeaf(result, 'source').activeTab, null)
  assert.equal(findLeaf(result, 'source').previewTab, null)
  assert.deepEqual(findLeaf(result, 'target').tabs, ['moving-preview'])
  assert.equal(findLeaf(result, 'target').activeTab, 'moving-preview')
  assert.equal(findLeaf(result, 'target').previewTab, 'moving-preview')
  assert.equal(listLeaves(result).length, 2)
  assert.equal(listSessionIds(result).filter((id) => id === 'moving-preview').length, 1)
})

test('moveSession replaces a target preview when moving another preview across panes', () => {
  const source = leaf(
    'source',
    ['source-fixed', 'moving-preview'],
    'moving-preview',
    'moving-preview',
  )
  const target = leaf(
    'target',
    ['target-fixed', 'displaced-preview', 'target-tail'],
    'displaced-preview',
    'displaced-preview',
  )
  const root = split('root', source, target)

  const result = moveSession(root, 'moving-preview', {
    sourceLeafId: 'source',
    targetLeafId: 'target',
    targetIndex: 2,
  })

  assert.deepEqual(findLeaf(result, 'source').tabs, ['source-fixed'])
  assert.equal(findLeaf(result, 'source').activeTab, 'source-fixed')
  assert.equal(findLeaf(result, 'source').previewTab, null)
  assert.deepEqual(
    findLeaf(result, 'target').tabs,
    ['target-fixed', 'moving-preview', 'target-tail'],
  )
  assert.equal(findLeaf(result, 'target').activeTab, 'moving-preview')
  assert.equal(findLeaf(result, 'target').previewTab, 'moving-preview')
  assert.equal(leafOfSession(result, 'displaced-preview'), null)
  assert.equal(listSessionIds(result).filter((id) => id === 'moving-preview').length, 1)
})

test('moveSession reorders pinned and preview tabs within one pane', () => {
  const pinnedRoot = leaf('pinned', ['first', 'middle', 'last'], 'first')
  const pinnedResult = moveSession(pinnedRoot, 'last', {
    sourceLeafId: 'pinned',
    targetLeafId: 'pinned',
    targetIndex: 0,
  })
  assert.deepEqual(pinnedResult.tabs, ['last', 'first', 'middle'])
  assert.equal(pinnedResult.activeTab, 'last')
  assert.equal(pinnedResult.previewTab, null)
  assert.equal(listSessionIds(pinnedResult).filter((id) => id === 'last').length, 1)

  const previewRoot = leaf(
    'preview',
    ['fixed-a', 'moving-preview', 'fixed-b'],
    'fixed-a',
    'moving-preview',
  )
  const previewResult = moveSession(previewRoot, 'moving-preview', {
    sourceLeafId: 'preview',
    targetLeafId: 'preview',
    targetIndex: 3,
  })
  assert.deepEqual(previewResult.tabs, ['fixed-a', 'fixed-b', 'moving-preview'])
  assert.equal(previewResult.activeTab, 'moving-preview')
  assert.equal(previewResult.previewTab, 'moving-preview')
  assert.equal(listSessionIds(previewResult).filter((id) => id === 'moving-preview').length, 1)
})

test('moveSession rejects invalid ownership and preserves identity for a logical no-op', () => {
  const left = leaf('left', ['left-a', 'left-b'], 'left-a')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  assert.strictEqual(moveSession(root, 'left-a', {
    sourceLeafId: 'missing',
    targetLeafId: 'right',
    targetIndex: 0,
  }), root)
  assert.strictEqual(moveSession(root, 'left-a', {
    sourceLeafId: 'left',
    targetLeafId: 'missing',
    targetIndex: 0,
  }), root)
  assert.strictEqual(moveSession(root, 'left-a', {
    sourceLeafId: 'right',
    targetLeafId: 'left',
    targetIndex: 0,
  }), root)
  assert.strictEqual(moveSession(root, 'not-owned', {
    sourceLeafId: 'left',
    targetLeafId: 'right',
    targetIndex: 0,
  }), root)
  for (const invalidIndex of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
    assert.strictEqual(moveSession(root, 'left-a', {
      sourceLeafId: 'left',
      targetLeafId: 'right',
      targetIndex: invalidIndex,
    }), root)
  }

  const noOp = moveSession(root, 'left-a', {
    sourceLeafId: 'left',
    targetLeafId: 'left',
    targetIndex: 0,
  })
  assert.strictEqual(noOp, root)
  assert.strictEqual(findLeaf(noOp, 'left'), left)
  assert.strictEqual(findLeaf(noOp, 'right'), right)
})

test('serializeLayout persists moved pinned order but drops a moved transient preview', () => {
  const pinnedSource = leaf('pinned-source', ['moving-pinned'], 'moving-pinned')
  const target = leaf(
    'target',
    ['target-fixed', 'transient-preview'],
    'transient-preview',
    'transient-preview',
  )
  const firstRoot = split('root', pinnedSource, target)
  const withMovedPinned = moveSession(firstRoot, 'moving-pinned', {
    sourceLeafId: 'pinned-source',
    targetLeafId: 'target',
    targetIndex: 1,
  })

  const transientSource = leaf(
    'transient-source',
    ['source-fixed', 'moving-preview'],
    'moving-preview',
    'moving-preview',
  )
  const combined = split('outer', withMovedPinned, transientSource, 'column')
  const withMovedPreview = moveSession(combined, 'moving-preview', {
    sourceLeafId: 'transient-source',
    targetLeafId: 'pinned-source',
    targetIndex: 0,
  })

  const parsed = parseLayout(serializeLayout(withMovedPreview, 'target'))
  assert.ok(parsed)
  assert.deepEqual(findLeaf(parsed.layout, 'pinned-source'), {
    type: 'leaf',
    id: 'pinned-source',
    tabs: [],
    activeTab: null,
    previewTab: null,
  })
  assert.deepEqual(findLeaf(parsed.layout, 'target'), {
    type: 'leaf',
    id: 'target',
    tabs: ['target-fixed', 'moving-pinned'],
    activeTab: 'moving-pinned',
    previewTab: null,
  })
  assert.deepEqual(findLeaf(parsed.layout, 'transient-source'), {
    type: 'leaf',
    id: 'transient-source',
    tabs: ['source-fixed'],
    activeTab: 'source-fixed',
    previewTab: null,
  })
  assert.equal(leafOfSession(parsed.layout, 'moving-preview'), null)
  assert.equal(listSessionIds(parsed.layout).filter((id) => id === 'moving-pinned').length, 1)
})

test('detachSession is pane-strict, selects a deterministic neighbor, and preserves an empty pane', () => {
  const left = leaf('left', ['first', 'active-preview', 'last'], 'active-preview', 'active-preview')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  assert.strictEqual(detachSession(root, 'right', 'active-preview'), root)
  assert.strictEqual(detachSession(root, 'left', 'missing'), root)

  const withoutPreview = detachSession(root, 'left', 'active-preview')
  const nextLeft = findLeaf(withoutPreview, 'left')
  assert.deepEqual(nextLeft.tabs, ['first', 'last'])
  assert.equal(nextLeft.activeTab, 'last')
  assert.equal(nextLeft.previewTab, null)
  assert.strictEqual(findLeaf(withoutPreview, 'right'), right)

  const withoutInactive = detachSession(withoutPreview, 'left', 'first')
  assert.deepEqual(findLeaf(withoutInactive, 'left').tabs, ['last'])
  assert.equal(findLeaf(withoutInactive, 'left').activeTab, 'last')

  const empty = detachSession(withoutInactive, 'left', 'last')
  assert.deepEqual(findLeaf(empty, 'left').tabs, [])
  assert.equal(findLeaf(empty, 'left').activeTab, null)
  assert.equal(findLeaf(empty, 'left').previewTab, null)
  assert.equal(listLeaves(empty).length, 2)
})

test('removeSession clears a preview marker and leaves an empty editor group after the final tab', () => {
  const left = leaf('left', ['only'], 'only', 'only')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  const next = removeSession(root, 'only')
  assert.equal(next.type, 'split')
  assert.deepEqual(findLeaf(next, 'left').tabs, [])
  assert.equal(findLeaf(next, 'left').activeTab, null)
  assert.equal(findLeaf(next, 'left').previewTab, null)
  assert.deepEqual(findLeaf(next, 'right').tabs, ['right-a'])
  assert.strictEqual(findLeaf(next, 'right'), right)
  assert.strictEqual(removeSession(next, 'not-open'), next)
})

test('closeLeaf collapses only the pane and detaches both preview and pinned sessions', () => {
  const left = leaf('left', ['left-a'], 'left-a')
  const closing = leaf('closing', ['closed-a', 'closed-preview'], 'closed-preview', 'closed-preview')
  const survivor = leaf('survivor', ['stay-a', 'stay-preview'], 'stay-a', 'stay-preview')
  const nested = split('nested', closing, survivor, 'column', 0.4)
  const root = split('root', left, nested, 'row', 0.6)

  const result = closeLeaf(root, 'closing')
  assert.ok(result)
  assert.equal(result.focusedLeafId, 'survivor')
  assert.deepEqual(
    listLeaves(result.root).map((item) => item.id),
    ['left', 'survivor'],
  )
  assert.deepEqual(findLeaf(result.root, 'left').tabs, ['left-a'])
  assert.deepEqual(findLeaf(result.root, 'survivor').tabs, ['stay-a', 'stay-preview'])
  assert.equal(findLeaf(result.root, 'survivor').previewTab, 'stay-preview')
  assert.equal(leafOfSession(result.root, 'closed-a'), null)
  assert.equal(leafOfSession(result.root, 'closed-preview'), null)
  assert.deepEqual(listSessionIds(result.root), ['left-a', 'stay-a', 'stay-preview'])

  assert.equal(closeLeaf(root, 'missing'), null)
  assert.equal(closeLeaf(left, 'left'), null)
})

test('reconcile removes stale assignments, clears preview markers, and never adopts server-side orphans', () => {
  const left = leaf('left', ['keep', 'stale-preview'], 'stale-preview', 'stale-preview')
  const right = leaf('right', ['stale-pinned', 'keep-preview'], 'stale-pinned', 'keep-preview')
  const root = split('root', left, right)

  const next = reconcile(root, ['keep', 'keep-preview', 'orphan'])
  assert.ok(next)
  assert.equal(next.type, 'split')
  assert.deepEqual(findLeaf(next, 'left').tabs, ['keep'])
  assert.equal(findLeaf(next, 'left').activeTab, 'keep')
  assert.equal(findLeaf(next, 'left').previewTab, null)
  assert.deepEqual(findLeaf(next, 'right').tabs, ['keep-preview'])
  assert.equal(findLeaf(next, 'right').activeTab, 'keep-preview')
  assert.equal(findLeaf(next, 'right').previewTab, 'keep-preview')
  assert.equal(leafOfSession(next, 'orphan'), null)
  assert.deepEqual(listSessionIds(next), ['keep', 'keep-preview'])

  assert.strictEqual(reconcile(next, ['keep', 'keep-preview', 'orphan']), next)

  const allRemoved = reconcile(next, ['orphan'])
  assert.equal(allRemoved.type, 'split')
  assert.deepEqual(
    listLeaves(allRemoved).map((item) => [item.id, item.tabs, item.activeTab, item.previewTab]),
    [
      ['left', [], null, null],
      ['right', [], null, null],
    ],
  )
  assert.equal(reconcile(null, ['orphan']), null)
})

test('splitLeaf rejects missing targets and creates an empty pane without changing the source pane', () => {
  const left = leaf('left', ['left-a', 'left-preview'], 'left-preview', 'left-preview')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  assert.equal(splitLeaf(root, 'missing', 'row'), null)

  const valid = splitLeaf(root, 'left', 'column')
  assert.ok(valid)
  assert.equal(listLeaves(valid.root).length, 3)
  assert.deepEqual(findLeaf(valid.root, valid.newLeafId), {
    type: 'leaf',
    id: valid.newLeafId,
    tabs: [],
    activeTab: null,
    previewTab: null,
  })
  assert.strictEqual(findLeaf(valid.root, 'left'), left)

  const splitEmpty = splitLeaf(leaf('empty'), 'empty', 'row')
  assert.ok(splitEmpty)
  assert.deepEqual(
    listLeaves(splitEmpty.root).map((item) => [item.tabs, item.activeTab, item.previewTab]),
    [
      [[], null, null],
      [[], null, null],
    ],
  )
})

test('serializeLayout drops transient previews without mutating runtime layout', () => {
  const left = leaf('left', ['fixed', 'left-preview'], 'left-preview', 'left-preview')
  const right = leaf('right', ['only-preview'], 'only-preview', 'only-preview')
  const root = split('root', left, right, 'column', 0.35)

  const serialized = serializeLayout(root, 'right')
  const payload = JSON.parse(serialized)
  assert.deepEqual(findLeaf(payload.layout, 'left'), {
    type: 'leaf',
    id: 'left',
    tabs: ['fixed'],
    activeTab: 'fixed',
    previewTab: null,
  })
  assert.deepEqual(findLeaf(payload.layout, 'right'), {
    type: 'leaf',
    id: 'right',
    tabs: [],
    activeTab: null,
    previewTab: null,
  })
  assert.deepEqual(left.tabs, ['fixed', 'left-preview'])
  assert.equal(left.activeTab, 'left-preview')
  assert.equal(left.previewTab, 'left-preview')
  assert.deepEqual(right.tabs, ['only-preview'])
  assert.equal(payload.focusedLeafId, 'right')

  const parsed = parseLayout(serialized)
  assert.ok(parsed)
  assert.deepEqual(parsed.layout, payload.layout)
})

test('parseLayout round-trips pinned tabs and migrates old v1 leaves as pinned', () => {
  const empty = leaf('empty')
  const populated = leaf('populated', ['one', 'two'], 'two')
  const root = split('root', empty, populated, 'column', 0.35)

  const parsed = parseLayout(serializeLayout(root, 'empty'))
  assert.deepEqual(parsed, { version: 1, focusedLeafId: 'empty', layout: root })

  const invalidFocus = parseLayout(serializeLayout(root, 'not-a-leaf'))
  assert.ok(invalidFocus)
  assert.equal(invalidFocus.focusedLeafId, null)
  assert.deepEqual(invalidFocus.layout, root)

  const legacyRoot = {
    type: 'split',
    id: 'legacy-root',
    direction: 'row',
    ratio: 0.5,
    children: [
      { type: 'leaf', id: 'legacy-left', tabs: ['old-a', 'old-b'], activeTab: 'old-b' },
      { type: 'leaf', id: 'legacy-empty', tabs: [], activeTab: null },
    ],
  }
  const migrated = parseLayout(JSON.stringify({
    version: 1,
    focusedLeafId: 'legacy-left',
    layout: legacyRoot,
  }))
  assert.ok(migrated)
  assert.deepEqual(findLeaf(migrated.layout, 'legacy-left'), {
    ...legacyRoot.children[0],
    previewTab: null,
  })
  assert.deepEqual(findLeaf(migrated.layout, 'legacy-empty'), {
    ...legacyRoot.children[1],
    previewTab: null,
  })

  // A valid marker found in storage is normalized to pinned because previews
  // are runtime-only and must never be restored after reload.
  const storedPreview = parseLayout(JSON.stringify({
    version: 1,
    focusedLeafId: 'stored',
    layout: leaf('stored', ['stored-tab'], 'stored-tab', 'stored-tab'),
  }))
  assert.ok(storedPreview)
  assert.deepEqual(storedPreview.layout.tabs, ['stored-tab'])
  assert.equal(storedPreview.layout.previewTab, null)
})

test('parseLayout rejects invalid activeTab and previewTab values plus duplicate IDs', () => {
  const payload = (root) => JSON.stringify({ version: 1, focusedLeafId: null, layout: root })

  assert.equal(parseLayout(payload(leaf('empty', [], 'ghost'))), null)
  assert.equal(parseLayout(payload(leaf('populated', ['one'], null))), null)
  assert.equal(parseLayout(payload(leaf('populated', ['one'], 'ghost'))), null)
  assert.equal(parseLayout(payload(leaf('empty-preview', [], null, 'ghost'))), null)
  assert.equal(parseLayout(payload(leaf('missing-preview', ['one'], 'one', 'ghost'))), null)
  assert.equal(parseLayout(payload(leaf('blank-preview', ['one'], 'one', '   '))), null)
  assert.equal(parseLayout(payload({
    type: 'leaf',
    id: 'typed-preview',
    tabs: ['one'],
    activeTab: 'one',
    previewTab: 42,
  })), null)

  const duplicateSession = split(
    'root',
    leaf('left', ['same'], 'same'),
    leaf('right', ['same'], 'same'),
  )
  assert.equal(parseLayout(payload(duplicateSession)), null)

  const duplicateNode = split(
    'root',
    leaf('same-leaf', ['one'], 'one'),
    leaf('same-leaf', ['two'], 'two'),
  )
  assert.equal(parseLayout(payload(duplicateNode)), null)
})
