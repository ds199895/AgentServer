import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/execution-contract.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const contract = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

test('404 and 501 disable execution without disabling the terminal UI', () => {
  assert.equal(contract.isExecutionUnavailableStatus(404), true)
  assert.equal(contract.isExecutionUnavailableStatus(501), true)
  assert.equal(contract.isExecutionUnavailableStatus(401), false)
  assert.equal(contract.isExecutionUnavailableStatus(503), false)
})

test('4401 is the only websocket close that permanently stops reconnect', () => {
  assert.equal(contract.shouldReconnectExecutionSocket(4401), false)
  assert.equal(contract.shouldReconnectExecutionSocket(1000), true)
  assert.equal(contract.shouldReconnectExecutionSocket(1012), true)
})

test('execution websocket always carries a normalized replay cursor', () => {
  assert.equal(contract.executionSocketPath(42), '/ws/execution?after_sequence=42')
  assert.equal(contract.executionSocketPath(-1), '/ws/execution?after_sequence=0')
  assert.equal(contract.executionSocketPath(Number.NaN), '/ws/execution?after_sequence=0')
})
