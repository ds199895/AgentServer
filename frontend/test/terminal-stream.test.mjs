import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/terminal-stream.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const { isSnapshotProtocolReply, RecoveryInputBuffer } = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

test('snapshot-generated terminal replies are filtered from recovery input', () => {
  for (const reply of [
    '\x1b[?1;2c',
    '\x1b[>0;276;0c',
    '\x1b[0n',
    '\x1b[12;24R',
    '\x1b[?1;2$y',
    '\x1b[4;768;1024t',
    '\x1b]10;rgb:ffff/ffff/ffff\x1b\\',
    '\x1bP1$r0m\x1b\\',
  ]) {
    assert.equal(isSnapshotProtocolReply(reply), true, JSON.stringify(reply))
  }
})

test('ordinary typing, delete and navigation remain recoverable user input', () => {
  for (const input of ['hello', '\x7f', '\x1b[A', '\x1b[3~', '123c', '中文']) {
    assert.equal(isSnapshotProtocolReply(input), false, JSON.stringify(input))
  }
})

test('recovery input is bounded, ordered and reusable after draining', () => {
  const buffer = new RecoveryInputBuffer(8)
  assert.equal(buffer.push('ab'), true)
  assert.equal(buffer.push('中文'), true)
  assert.equal(buffer.byteLength, 8)
  assert.equal(buffer.push('x'), false)
  assert.deepEqual(
    buffer.drain().map((chunk) => new TextDecoder().decode(chunk)),
    ['ab', '中文'],
  )
  assert.equal(buffer.byteLength, 0)
  assert.equal(buffer.push('next'), true)
  buffer.clear()
  assert.equal(buffer.byteLength, 0)
})

test('recovery input rejects invalid limits', () => {
  assert.throws(() => new RecoveryInputBuffer(0), /positive integer/)
  assert.throws(() => new RecoveryInputBuffer(1.5), /positive integer/)
})
