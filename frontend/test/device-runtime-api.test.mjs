import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

globalThis.__AGENTSERVER_BUILD_SHA__ = 'test'

const apiSourceUrl = new URL('../src/api.ts', import.meta.url)
const apiSource = await readFile(apiSourceUrl, 'utf8')
const transpiled = await transformWithOxc(apiSource, apiSourceUrl.pathname, { lang: 'ts' })
const runtime = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

function jsonResponse(value) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('runtime API encodes scope, forwards AbortSignal, and carries stable operation ids', async (t) => {
  const calls = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (path, init) => {
    calls.push({ path, init })
    return jsonResponse({
      device_id: 'device/a',
      enrollment_token: 'one-time-secret',
      expires_at: 1,
      sessions: [],
      events: [],
      session: { id: 'session/1', device_id: 'device/a' },
      command: {},
      ok: true,
    })
  }
  t.after(() => { globalThis.fetch = originalFetch })

  const controller = new AbortController()
  const options = { signal: controller.signal }
  await runtime.api.createRuntimeEnrollment('device/a', options)
  await runtime.api.probeDeviceRuntime('device/a', options)
  await runtime.api.runtimeSessions('device/a', options)
  await runtime.api.createRuntimeSession('device/a', {
    session_id: 'stable-session-id',
    provider: 'codex',
    cwd: '/workspace',
    permission_mode: 'workspace-write',
    model: null,
  }, options)
  await runtime.api.runtimeSessionEvents('session/1', 12, options)
  await runtime.api.startRuntimeTurn('session/1', 'finish the task', null, 'stable-turn-id', options)
  await runtime.api.interruptRuntimeTurn('session/1', options)
  await runtime.api.stopRuntimeSession('session/1', options)
  await runtime.api.respondRuntimeInteraction('session/1', 'request/1', { decision: 'deny' }, options)
  await runtime.api.revokeDeviceRuntime('device/a', options)

  assert.deepEqual(calls.map((call) => call.path), [
    '/api/devices/device%2Fa/runtime/enrollment-tokens',
    '/api/devices/device%2Fa/runtime/probe',
    '/api/devices/device%2Fa/runtime/sessions',
    '/api/devices/device%2Fa/runtime/sessions',
    '/api/runtime-sessions/session%2F1/events?after_sequence=12',
    '/api/runtime-sessions/session%2F1/turns',
    '/api/runtime-sessions/session%2F1/interrupt',
    '/api/runtime-sessions/session%2F1',
    '/api/runtime-sessions/session%2F1/interactions/request%2F1/respond',
    '/api/devices/device%2Fa/runtime/credential',
  ])
  assert.deepEqual(calls.map((call) => call.init.method || 'GET'), [
    'POST', 'POST', 'GET', 'POST', 'GET', 'POST', 'POST', 'DELETE', 'POST', 'DELETE',
  ])
  assert.ok(calls.every((call) => call.init.signal === controller.signal))
  assert.equal(calls[0].init.cache, 'no-store')
  assert.equal(JSON.parse(calls[0].init.body).ttl_seconds, 30 * 60)
  assert.equal(JSON.parse(calls[3].init.body).session_id, 'stable-session-id')
  assert.equal(JSON.parse(calls[5].init.body).turn_id, 'stable-turn-id')
})

test('runtime scope guards reject cross-device sessions and events', () => {
  const session = { device_id: 'device-a' }
  assert.equal(runtime.isRuntimeSessionForDevice(session, 'device-a'), true)
  assert.equal(runtime.isRuntimeSessionForDevice(session, 'device-b'), false)

  const event = { device_id: 'device-a', session_id: 'session-a' }
  assert.equal(runtime.isRuntimeEventForSession(event, 'device-a', 'session-a'), true)
  assert.equal(runtime.isRuntimeEventForSession(event, 'device-b', 'session-a'), false)
  assert.equal(runtime.isRuntimeEventForSession(event, 'device-a', 'session-b'), false)
})

test('runtime socket URL uses the current secure origin and normalizes its cursor', () => {
  const originalWindow = globalThis.window
  globalThis.window = { location: { protocol: 'https:', host: 'agentserver.test' } }
  try {
    assert.equal(
      runtime.runtimeSessionSocketUrl('session/a', -12),
      'wss://agentserver.test/ws/runtime-sessions/session%2Fa?after_sequence=0',
    )
  } finally {
    globalThis.window = originalWindow
  }
})

test('runtime request ids are unique UUIDs and AbortError detection is cross-realm safe', () => {
  const first = runtime.newRuntimeRequestId()
  const second = runtime.newRuntimeRequestId()
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  assert.notEqual(first, second)
  assert.equal(runtime.isAbortError({ name: 'AbortError' }), true)
  assert.equal(runtime.isAbortError(new Error('network failure')), false)
})

test('ambiguous retries reuse an id until the operation fingerprint changes', () => {
  const first = runtime.runtimeRequestIdentityFor(null, 'same-operation')
  const retry = runtime.runtimeRequestIdentityFor(first, 'same-operation')
  const changed = runtime.runtimeRequestIdentityFor(retry, 'changed-operation')
  assert.equal(retry, first)
  assert.equal(retry.requestId, first.requestId)
  assert.notEqual(changed.requestId, first.requestId)
})

test('runtime dialog does not persist or log secrets and tears down asynchronous work', async () => {
  const dialog = await readFile(new URL('../src/components/DeviceRuntimeDialog.tsx', import.meta.url), 'utf8')
  const downloads = await readFile(new URL('../src/components/DownloadsPage.tsx', import.meta.url), 'utf8')
  const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(dialog, /\b(?:localStorage|sessionStorage)\b|console\./)
  assert.doesNotMatch(downloads, /\b(?:localStorage|sessionStorage)\b|console\./)
  assert.match(dialog, /setEnrollment\(null\)/)
  assert.match(dialog, /abortAllRequests\(\)/)
  assert.match(dialog, /controller\.abort\(\)/)
  assert.doesNotMatch(dialog, /setInterval\(/)
  assert.match(dialog, /pendingSessionCreateRef\.current = runtimeRequestIdentityFor/)
  assert.match(dialog, /runtimeRequestIdentityFor\(pendingTurnRef\.current/)
  assert.match(dialog, /runtimeSessionSocketUrl\(sessionId\)/)
  assert.match(dialog, /coalesceRuntimeEvents/)
  assert.match(dialog, /socketState === 'live'/)
  assert.doesNotMatch(dialog, /--runtime-user.*device\.ssh_user/)
  assert.doesNotMatch(downloads, /--runtime-user/)
  assert.match(downloads, /完整 Runtime bootstrap 由持有 Codex 登录态的 Linux 普通用户运行/)
  assert.match(downloads, /仅传统 SSH Shell 命令支持/)
  assert.match(downloads, /powershellQuote\(deviceId/)
  assert.match(downloads, /powershellQuote\(sshUser/)
  const commandSource = downloads.slice(
    downloads.indexOf('const bootstrapCommand'),
    downloads.indexOf('const mergeCommand'),
  )
  assert.doesNotMatch(commandSource, /enrollment|token/i)
  assert.match(downloads, /function SecretBox\(\{ value, copyLabel \}/)
  assert.match(downloads, /<SecretBox value=\{enrollment\.enrollment_token\}/)
  assert.match(downloads, /clearPreparedEnrollment\(\); setDeviceId/)
  assert.match(downloads, /clearPreparedEnrollment\(\); setPortDraft/)
  assert.match(downloads, /clearPreparedEnrollment\(\); setSshUser/)
  assert.match(downloads, /const beginPortEdit = \(\) => \{\s+clearPreparedEnrollment\(\)/)
  assert.match(app, /<DownloadsPage devices=\{devices\} onChanged=/)
  assert.match(app, /setUsername\(null\); setRuntimeDevice\(null\)/)
  assert.match(app, /onAdd=\{\(\) => setDeviceDialog\('new'\)\}/)
})
