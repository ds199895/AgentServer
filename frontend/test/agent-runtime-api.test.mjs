import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

globalThis.__AGENTSERVER_BUILD_SHA__ = 'test'

async function importTypescript(relativePath) {
  const sourceUrl = new URL(relativePath, import.meta.url)
  const source = await readFile(sourceUrl, 'utf8')
  const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
  return import(`data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`)
}

const agentRuntime = await importTypescript('../src/agent/api.ts')
const deviceRuntime = await importTypescript('../src/api.ts')

function jsonResponse(value) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('agent API encodes session and request ids and sends the expected payloads', async (t) => {
  const calls = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (path, init = {}) => {
    calls.push({ path, init })
    return jsonResponse({ sessions: [], session: {}, turn: {}, accepted: true })
  }
  t.after(() => { globalThis.fetch = originalFetch })

  await agentRuntime.agentApi.sessions()
  await agentRuntime.agentApi.create({
    session_id: 'stable-session-id',
    provider: 'codex',
    device_id: 'device/a',
    cwd: '/workspace',
    permission_mode: 'workspace-write',
    model: null,
  })
  await agentRuntime.agentApi.get('session/1')
  await agentRuntime.agentApi.turn('session/1', 'finish the task')
  await agentRuntime.agentApi.interrupt('session/1')
  await agentRuntime.agentApi.respond('session/1', 'request/1', { decision: 'deny' })

  assert.deepEqual(calls.map((call) => call.path), [
    '/api/agent/sessions',
    '/api/agent/sessions',
    '/api/agent/sessions/session%2F1',
    '/api/agent/sessions/session%2F1/turns',
    '/api/agent/sessions/session%2F1/interrupt',
    '/api/agent/sessions/session%2F1/requests/respond',
  ])
  assert.deepEqual(calls.map((call) => call.init.method || 'GET'), [
    'GET', 'POST', 'GET', 'POST', 'POST', 'POST',
  ])
  assert.equal(JSON.parse(calls[1].init.body).session_id, 'stable-session-id')
  assert.equal(JSON.parse(calls[3].init.body).input, 'finish the task')
  assert.equal(JSON.parse(calls[5].init.body).request_id, 'request/1')
  assert.deepEqual(JSON.parse(calls[5].init.body).payload, { decision: 'deny' })
})

test('agent socket URL uses the current secure origin and normalizes its cursor', () => {
  const originalWindow = globalThis.window
  globalThis.window = { location: { protocol: 'https:', host: 'agentserver.test' } }
  try {
    assert.equal(
      agentRuntime.agentSessionSocketUrl('session/a', -12),
      'wss://agentserver.test/ws/agent/sessions/session%2Fa?after_sequence=0',
    )
  } finally {
    globalThis.window = originalWindow
  }
})

test('device bootstrap API keeps enrollment credentials scoped and abortable', async (t) => {
  const calls = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (path, init = {}) => {
    calls.push({ path, init })
    return jsonResponse({ command: {}, ok: true })
  }
  t.after(() => { globalThis.fetch = originalFetch })

  const controller = new AbortController()
  const options = { signal: controller.signal }
  await deviceRuntime.api.createRuntimeEnrollment('device/a', options)
  await deviceRuntime.api.probeDeviceRuntime('device/a', options)
  await deviceRuntime.api.revokeDeviceRuntime('device/a', options)

  assert.deepEqual(calls.map((call) => call.path), [
    '/api/devices/device%2Fa/runtime/enrollment-tokens',
    '/api/devices/device%2Fa/runtime/probe',
    '/api/devices/device%2Fa/runtime/credential',
  ])
  assert.deepEqual(calls.map((call) => call.init.method), ['POST', 'POST', 'DELETE'])
  assert.ok(calls.every((call) => call.init.signal === controller.signal))
  assert.equal(calls[0].init.cache, 'no-store')
  assert.equal(JSON.parse(calls[0].init.body).ttl_seconds, 30 * 60)
})

test('application mounts the agent session pane and no longer references the legacy dialog', async () => {
  const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.match(app, /<AgentSessionPane sessionId=\{agentSessionId\}/)
  assert.doesNotMatch(app, /DeviceRuntimeDialog/)
  assert.match(app, /setUsername\(null\); setAgentSessionId\(null\)/)
})
