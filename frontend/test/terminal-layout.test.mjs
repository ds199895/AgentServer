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
  findLeaf,
  leafOfSession,
  listLeaves,
  listSessionIds,
  parseLayout,
  reconcile,
  removeSession,
  serializeLayout,
  splitLeaf,
} = layout

function leaf(id, tabs = [], activeTab = tabs[0] ?? null) {
  return { type: 'leaf', id, tabs, activeTab }
}

function split(id, first, second, direction = 'row', ratio = 0.5) {
  return { type: 'split', id, direction, ratio, children: [first, second] }
}

test('each leaf switches its activeTab independently', () => {
  const left = leaf('left', ['left-a', 'left-b'], 'left-a')
  const right = leaf('right', ['right-a', 'right-b'], 'right-a')
  const root = split('root', left, right)

  const leftResult = activateSession(root, 'left-b')
  assert.equal(leftResult.leafId, 'left')
  assert.equal(findLeaf(leftResult.root, 'left').activeTab, 'left-b')
  assert.equal(findLeaf(leftResult.root, 'right').activeTab, 'right-a')
  assert.strictEqual(findLeaf(leftResult.root, 'right'), right)

  const rightResult = activateSession(leftResult.root, 'right-b')
  assert.equal(rightResult.leafId, 'right')
  assert.equal(findLeaf(rightResult.root, 'left').activeTab, 'left-b')
  assert.equal(findLeaf(rightResult.root, 'right').activeTab, 'right-b')
})

test('activateSession reveals assigned sessions in their owner and assigns orphan sessions to the explicit target', () => {
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
  assert.equal(findLeaf(assign.root, 'left').activeTab, 'left-b')

  const invalidTarget = activateSession(root, 'fallback', { leafId: 'missing' })
  assert.equal(invalidTarget.leafId, 'left')
  assert.deepEqual(findLeaf(invalidTarget.root, 'left').tabs, ['left-a', 'left-b', 'fallback'])

  const bootstrap = activateSession(null, 'first')
  assert.deepEqual(bootstrap.root.tabs, ['first'])
  assert.equal(bootstrap.root.activeTab, 'first')
  assert.equal(bootstrap.leafId, bootstrap.root.id)
})

test('removeSession leaves an empty editor group when its final tab closes', () => {
  const left = leaf('left', ['only'], 'only')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  const next = removeSession(root, 'only')
  assert.equal(next.type, 'split')
  assert.deepEqual(findLeaf(next, 'left').tabs, [])
  assert.equal(findLeaf(next, 'left').activeTab, null)
  assert.deepEqual(findLeaf(next, 'right').tabs, ['right-a'])
  assert.strictEqual(findLeaf(next, 'right'), right)
  assert.strictEqual(removeSession(next, 'not-open'), next)
})

test('closeLeaf collapses only the pane and leaves its sessions unassigned instead of merging tabs', () => {
  const left = leaf('left', ['left-a'], 'left-a')
  const closing = leaf('closing', ['closed-a', 'closed-b'], 'closed-b')
  const survivor = leaf('survivor', ['stay-a', 'stay-b'], 'stay-a')
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
  assert.deepEqual(findLeaf(result.root, 'survivor').tabs, ['stay-a', 'stay-b'])
  assert.equal(leafOfSession(result.root, 'closed-a'), null)
  assert.equal(leafOfSession(result.root, 'closed-b'), null)
  assert.deepEqual(listSessionIds(result.root), ['left-a', 'stay-a', 'stay-b'])

  assert.equal(closeLeaf(root, 'missing'), null)
  assert.equal(closeLeaf(left, 'left'), null)
})

test('reconcile removes stale assignments, preserves empty leaves, and never adopts server-side orphans', () => {
  const left = leaf('left', ['keep', 'stale'], 'stale')
  const empty = leaf('empty')
  const root = split('root', left, empty)

  const next = reconcile(root, ['keep', 'orphan'])
  assert.ok(next)
  assert.equal(next.type, 'split')
  assert.deepEqual(findLeaf(next, 'left').tabs, ['keep'])
  assert.equal(findLeaf(next, 'left').activeTab, 'keep')
  assert.deepEqual(findLeaf(next, 'empty').tabs, [])
  assert.equal(findLeaf(next, 'empty').activeTab, null)
  assert.equal(leafOfSession(next, 'orphan'), null)
  assert.deepEqual(listSessionIds(next), ['keep'])

  assert.strictEqual(reconcile(next, ['keep', 'orphan']), next)

  const allRemoved = reconcile(next, ['orphan'])
  assert.equal(allRemoved.type, 'split')
  assert.deepEqual(
    listLeaves(allRemoved).map((item) => [item.id, item.tabs, item.activeTab]),
    [
      ['left', [], null],
      ['empty', [], null],
    ],
  )
  assert.equal(reconcile(null, ['orphan']), null)
})

test('splitLeaf rejects missing targets and duplicate session ownership', () => {
  const left = leaf('left', ['left-a'], 'left-a')
  const right = leaf('right', ['right-a'], 'right-a')
  const root = split('root', left, right)

  assert.equal(splitLeaf(root, 'missing', 'row', 'new-a'), null)
  assert.equal(splitLeaf(root, 'left', 'column', 'right-a'), null)

  const valid = splitLeaf(root, 'left', 'column', 'new-a')
  assert.ok(valid)
  assert.equal(listLeaves(valid.root).length, 3)
  assert.deepEqual(findLeaf(valid.root, valid.newLeafId).tabs, ['new-a'])
  assert.equal(findLeaf(valid.root, valid.newLeafId).activeTab, 'new-a')
  assert.deepEqual(findLeaf(valid.root, 'left').tabs, ['left-a'])
})

test('parseLayout round-trips empty leaves and valid per-pane active tabs', () => {
  const empty = leaf('empty')
  const populated = leaf('populated', ['one', 'two'], 'two')
  const root = split('root', empty, populated, 'column', 0.35)

  const parsed = parseLayout(serializeLayout(root, 'empty'))
  assert.deepEqual(parsed, { version: 1, focusedLeafId: 'empty', layout: root })

  const invalidFocus = parseLayout(serializeLayout(root, 'not-a-leaf'))
  assert.ok(invalidFocus)
  assert.equal(invalidFocus.focusedLeafId, null)
  assert.deepEqual(invalidFocus.layout, root)
})

test('parseLayout rejects invalid activeTab values and duplicate IDs', () => {
  const payload = (root) => JSON.stringify({ version: 1, focusedLeafId: null, layout: root })

  assert.equal(parseLayout(payload(leaf('empty', [], 'ghost'))), null)
  assert.equal(parseLayout(payload(leaf('populated', ['one'], null))), null)
  assert.equal(parseLayout(payload(leaf('populated', ['one'], 'ghost'))), null)

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
