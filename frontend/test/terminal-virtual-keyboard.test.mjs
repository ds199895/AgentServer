import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/terminal-virtual-keyboard.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const { encodeVirtualKey } = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

const none = { shift: false, ctrl: false, alt: false }

test('plain accessory keys preserve their terminal sequences', () => {
  assert.equal(encodeVirtualKey('escape', none), '\x1b')
  assert.equal(encodeVirtualKey('enter', none), '\r')
  assert.equal(encodeVirtualKey('tab', none), '\t')
  assert.equal(encodeVirtualKey('arrowUp', none), '\x1b[A')
  assert.equal(encodeVirtualKey('arrowUp', none, true), '\x1bOA')
})

test('latched modifiers use xterm-compatible key sequences', () => {
  assert.equal(
    encodeVirtualKey('arrowLeft', { shift: true, ctrl: false, alt: false }),
    '\x1b[1;2D',
  )
  assert.equal(
    encodeVirtualKey('arrowRight', { shift: false, ctrl: true, alt: false }),
    '\x1b[1;5C',
  )
  assert.equal(
    encodeVirtualKey('arrowDown', { shift: true, ctrl: true, alt: true }),
    '\x1b[1;8B',
  )
  assert.equal(
    encodeVirtualKey('tab', { shift: true, ctrl: false, alt: false }),
    '\x1b[Z',
  )
  assert.equal(
    encodeVirtualKey('enter', { shift: false, ctrl: false, alt: true }),
    '\x1b\r',
  )
})
