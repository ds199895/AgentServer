import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/device-bootstrap.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const runtime = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

function device(overrides = {}) {
  return {
    id: 'device-001',
    proxy_name: 'device-001.ssh',
    remote_port: 20001,
    ssh_user: 'operator',
    remote_shell: 'system',
    runtime: { state: 'unregistered' },
    ...overrides,
  }
}

function enrollment(overrides = {}) {
  return {
    device_id: 'device-001',
    enrollment_token: 'one-time-token',
    expires_at: 1_900_000_000,
    ...overrides,
  }
}

const draft = { deviceId: 'device-001', remotePort: 20001, sshUser: 'operator' }

test('bootstrap curl permits initial HTTP only for exact loopback origins', () => {
  assert.equal(
    runtime.bootstrapCurlProtocolArgs('https://agentserver.example.com'),
    "--proto '=https' --proto-redir '=https'",
  )
  for (const origin of [
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'http://[::1]:8000',
  ]) {
    assert.equal(
      runtime.bootstrapCurlProtocolArgs(origin),
      "--proto '=https,http' --proto-redir '=https'",
    )
  }
  for (const origin of ['http://agentserver.example.com', 'not a URL']) {
    assert.equal(
      runtime.bootstrapCurlProtocolArgs(origin),
      "--proto '=https' --proto-redir '=https'",
    )
  }
})

test('new device registration fixes the FRP proxy identity before enrollment', async () => {
  const calls = []
  const client = {
    async createDevice(input) {
      calls.push(['create', input])
      return device()
    },
    async devices() {
      calls.push(['list'])
      return []
    },
    async createRuntimeEnrollment(deviceId) {
      calls.push(['enroll', deviceId])
      return enrollment()
    },
  }

  const prepared = await runtime.prepareDeviceEnrollment(client, [], draft)

  assert.deepEqual(calls.map(([operation]) => operation), ['create', 'enroll'])
  assert.equal(calls[0][1].proxy_name, 'device-001.ssh')
  assert.equal(calls[0][1].remote_shell, 'system')
  assert.equal(prepared.device.id, 'device-001')
  assert.equal(prepared.enrollment.enrollment_token, 'one-time-token')
})

test('an exact unregistered device is reused without creating a duplicate', async () => {
  let createCalls = 0
  let enrollCalls = 0
  const client = {
    async createDevice() { createCalls += 1; throw new Error('unexpected create') },
    async devices() { throw new Error('unexpected list') },
    async createRuntimeEnrollment() { enrollCalls += 1; return enrollment() },
  }

  const prepared = await runtime.prepareDeviceEnrollment(client, [device()], draft)

  assert.equal(createCalls, 0)
  assert.equal(enrollCalls, 1)
  assert.equal(prepared.input.proxy_name, 'device-001.ssh')
})

test('a concurrent matching registration is recovered after HTTP 409', async () => {
  const calls = []
  const conflict = Object.assign(new Error('conflict'), { status: 409 })
  const client = {
    async createDevice() { calls.push('create'); throw conflict },
    async devices() { calls.push('list'); return [device()] },
    async createRuntimeEnrollment() { calls.push('enroll'); return enrollment() },
  }

  await runtime.prepareDeviceEnrollment(client, [], draft)

  assert.deepEqual(calls, ['create', 'list', 'enroll'])
})

test('inventory conflicts and active Runtime credentials fail before enrollment', async () => {
  let enrollCalls = 0
  const client = {
    async createDevice() { throw new Error('unexpected create') },
    async devices() { return [] },
    async createRuntimeEnrollment() { enrollCalls += 1; return enrollment() },
  }

  await assert.rejects(
    runtime.prepareDeviceEnrollment(client, [device({ remote_port: 20002 })], draft),
    /参数.*不一致|端口.*不一致/,
  )
  await assert.rejects(
    runtime.prepareDeviceEnrollment(client, [device({ runtime: { state: 'online' } })], draft),
    /已有 Runtime 凭据/,
  )
  assert.equal(enrollCalls, 0)
})

test('a 409 owned by another device and a mismatched token response fail closed', async () => {
  const conflict = Object.assign(new Error('conflict'), { status: 409 })
  await assert.rejects(
    runtime.prepareDeviceEnrollment({
      async createDevice() { throw conflict },
      async devices() { return [device({ id: 'other-device' })] },
      async createRuntimeEnrollment() { throw new Error('unexpected enrollment') },
    }, [], draft),
    /已被其他设备占用/,
  )
  await assert.rejects(
    runtime.prepareDeviceEnrollment({
      async createDevice() { return device() },
      async devices() { return [] },
      async createRuntimeEnrollment() { return enrollment({ device_id: 'other-device' }) },
    }, [], draft),
    /无效或不匹配/,
  )
})

test('registration validates installer-compatible identities locally', () => {
  assert.throws(
    () => runtime.registrationInput({ ...draft, deviceId: 'bad device' }),
    /设备 ID/,
  )
  assert.throws(
    () => runtime.registrationInput({ ...draft, sshUser: 'bad user' }),
    /SSH 用户/,
  )
  assert.throws(
    () => runtime.registrationInput({ ...draft, remotePort: 19999 }),
    /20000-29999/,
  )
})
