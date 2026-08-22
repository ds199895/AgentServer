import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/terminal-input-bridge.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const { attachNativeInputBridge, configureTerminalInputTextarea } = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

class FakeTextarea extends EventTarget {
  dataset = {}
  autocapitalize = ''
  autocomplete = ''
  autocorrect = ''
  spellcheck = true

  setAttribute(name, value) {
    this[name] = value
  }
}

function inputEvent(type, data, inputType) {
  const event = new Event(type)
  Object.defineProperties(event, {
    data: { value: data },
    inputType: { value: inputType },
  })
  return event
}

function scheduler() {
  let next = 0
  const pending = new Map()
  return {
    schedule(callback) {
      next += 1
      pending.set(next, callback)
      return next
    },
    cancel(handle) {
      pending.delete(handle)
    },
    flushOne() {
      const entry = pending.entries().next().value
      if (!entry) return false
      const [handle, callback] = entry
      pending.delete(handle)
      callback()
      return true
    },
    flushAll() {
      while (this.flushOne()) {}
    },
  }
}

function setup() {
  const textarea = new FakeTextarea()
  const sent = []
  const clock = scheduler()
  const bridge = attachNativeInputBridge(textarea, {
    onFallbackText: (data) => sent.push(data),
    schedule: (callback) => clock.schedule(callback),
    cancel: (handle) => clock.cancel(handle),
  })
  return { textarea, sent, clock, bridge }
}

test('forwards missing iOS Chinese punctuation exactly once', () => {
  const { textarea, sent, clock } = setup()
  textarea.dispatchEvent(inputEvent('beforeinput', '。', 'insertText'))
  textarea.dispatchEvent(inputEvent('input', '。', 'insertText'))
  clock.flushAll()
  assert.deepEqual(sent, ['。'])
})

test('does not duplicate text xterm already emitted', () => {
  const { textarea, sent, clock, bridge } = setup()
  textarea.dispatchEvent(inputEvent('beforeinput', '“', 'insertText'))
  bridge.noteXtermData('“')
  clock.flushAll()
  assert.deepEqual(sent, [])
})

test('waits for committed composition text and never sends updates', () => {
  const { textarea, sent, clock } = setup()
  textarea.dispatchEvent(new Event('compositionstart'))
  textarea.dispatchEvent(new Event('compositionupdate'))
  textarea.dispatchEvent(inputEvent('beforeinput', '中。', 'insertFromComposition'))
  clock.flushOne()
  assert.deepEqual(sent, [])
  assert.equal(textarea.dataset.nativeComposing, 'true')
  textarea.dispatchEvent(new Event('compositionend'))
  clock.flushAll()
  assert.deepEqual(sent, ['中。'])
  assert.equal(textarea.dataset.nativeComposing, 'false')
})

test('ignores deletion, paste, and empty input data', () => {
  const { textarea, sent, clock } = setup()
  textarea.dispatchEvent(inputEvent('beforeinput', null, 'deleteContentBackward'))
  textarea.dispatchEvent(inputEvent('beforeinput', 'paste', 'insertFromPaste'))
  textarea.dispatchEvent(inputEvent('input', '', 'insertText'))
  clock.flushAll()
  assert.deepEqual(sent, [])
})

test('preserves rapid committed strings in order', () => {
  const { textarea, sent, clock } = setup()
  textarea.dispatchEvent(inputEvent('beforeinput', '。', 'insertText'))
  textarea.dispatchEvent(inputEvent('beforeinput', '”', 'insertText'))
  clock.flushAll()
  assert.deepEqual(sent, ['。', '”'])
})

test('disposing removes native listeners and pending fallback', () => {
  const { textarea, sent, clock, bridge } = setup()
  textarea.dispatchEvent(inputEvent('beforeinput', '。', 'insertText'))
  bridge.dispose()
  clock.flushAll()
  textarea.dispatchEvent(inputEvent('beforeinput', '“', 'insertText'))
  clock.flushAll()
  assert.deepEqual(sent, [])
})

test('sets mobile-safe helper textarea attributes without forcing an input mode', () => {
  const textarea = new FakeTextarea()
  configureTerminalInputTextarea(textarea)
  assert.equal(textarea.autocapitalize, 'none')
  assert.equal(textarea.autocomplete, 'off')
  assert.equal(textarea.autocorrect, 'off')
  assert.equal(textarea.spellcheck, false)
  assert.equal('inputMode' in textarea, false)
})
