import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/pwa-viewport-anchor.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const { computeViewportAnchorTransform } = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

test('counter-translates a panned viewport while a terminal is focused in a PWA', () => {
  assert.equal(
    computeViewportAnchorTransform({
      standalone: true,
      terminalFocused: true,
      offsetLeft: 0,
      offsetTop: 312,
    }),
    'translate(0px, -312px)',
  )
})

test('does nothing when the viewport is not panned', () => {
  assert.equal(
    computeViewportAnchorTransform({
      standalone: true,
      terminalFocused: true,
      offsetLeft: 0,
      offsetTop: 0,
    }),
    '',
  )
})

test('does nothing outside the installed PWA (Safari keeps native behavior)', () => {
  assert.equal(
    computeViewportAnchorTransform({
      standalone: false,
      terminalFocused: true,
      offsetLeft: 0,
      offsetTop: 312,
    }),
    '',
  )
})

test('does nothing for non-terminal inputs so soft keyboards can still pan them', () => {
  assert.equal(
    computeViewportAnchorTransform({
      standalone: true,
      terminalFocused: false,
      offsetLeft: 0,
      offsetTop: 312,
    }),
    '',
  )
})
