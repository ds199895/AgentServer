export type DeviceRegistrationDraft = {
  deviceId: string
  remotePort: number
  sshUser: string
}

export type DeviceRegistrationInput = {
  id: string
  name: string
  proxy_name: string
  remote_port: number
  ssh_user: string
  remote_shell: 'system'
  notes: string
}

export type RegisteredDevice = {
  id: string
  proxy_name: string
  remote_port: number
  ssh_user: string
  remote_shell: string
  runtime?: { state?: string } | null
}

export type RuntimeEnrollment = {
  device_id: string
  enrollment_token: string
  expires_at: number
}

export type DeviceRegistrationClient = {
  createDevice(input: DeviceRegistrationInput): Promise<RegisteredDevice>
  devices(): Promise<RegisteredDevice[]>
  createRuntimeEnrollment(deviceId: string): Promise<RuntimeEnrollment>
}

export type PreparedDeviceEnrollment = {
  device: RegisteredDevice
  enrollment: RuntimeEnrollment
  input: DeviceRegistrationInput
}

export class DeviceRegistrationConflictError extends Error {}

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])

export function bootstrapCurlProtocolArgs(origin: string): string {
  try {
    const url = new URL(origin)
    if (url.protocol === 'http:' && LOOPBACK_HOSTS.has(url.hostname)) {
      return "--proto '=https,http' --proto-redir '=https'"
    }
  } catch {
    // A malformed or unsupported origin fails closed to HTTPS-only downloads.
  }
  return "--proto '=https' --proto-redir '=https'"
}

export function registrationInput(
  draft: DeviceRegistrationDraft,
): DeviceRegistrationInput {
  const id = draft.deviceId.trim()
  const sshUser = draft.sshUser.trim()
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$/.test(id)) {
    throw new Error('设备 ID 需为 2-64 位字母、数字、点、下划线或连字符')
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(sshUser)) {
    throw new Error('SSH 用户格式无效')
  }
  if (!Number.isInteger(draft.remotePort)
    || draft.remotePort < 20000
    || draft.remotePort > 29999) {
    throw new Error('远端端口必须位于 20000-29999')
  }
  return {
    id,
    name: id,
    proxy_name: `${id}.ssh`,
    remote_port: draft.remotePort,
    ssh_user: sshUser,
    remote_shell: 'system',
    notes: '',
  }
}

function matchesRegistration(
  device: RegisteredDevice,
  expected: DeviceRegistrationInput,
): boolean {
  return device.id === expected.id
    && device.proxy_name === expected.proxy_name
    && device.remote_port === expected.remote_port
    && device.ssh_user === expected.ssh_user
    && device.remote_shell === expected.remote_shell
}

function conflict(message: string): never {
  throw new DeviceRegistrationConflictError(message)
}

function errorStatus(reason: unknown): number | undefined {
  if (!reason || typeof reason !== 'object' || !('status' in reason)) return undefined
  return typeof reason.status === 'number' ? reason.status : undefined
}

function validateReusableDevice(
  device: RegisteredDevice,
  expected: DeviceRegistrationInput,
): RegisteredDevice {
  if (!matchesRegistration(device, expected)) {
    conflict('同名设备已存在，但 FRP 端口、代理名、SSH 用户或系统类型不一致')
  }
  const runtimeState = device.runtime?.state
  if (runtimeState && !['unregistered', 'revoked'].includes(runtimeState)) {
    conflict('该设备已有 Runtime 凭据；请从设备 Runtime 面板管理或撤销后再重新接入')
  }
  return device
}

export async function prepareDeviceEnrollment(
  client: DeviceRegistrationClient,
  knownDevices: RegisteredDevice[],
  draft: DeviceRegistrationDraft,
): Promise<PreparedDeviceEnrollment> {
  const input = registrationInput(draft)
  let device = knownDevices.find((item) => item.id === input.id)
  if (device) {
    device = validateReusableDevice(device, input)
  } else {
    try {
      device = await client.createDevice(input)
    } catch (reason) {
      if (errorStatus(reason) !== 409) throw reason
      const refreshed = await client.devices()
      const racedDevice = refreshed.find((item) => item.id === input.id)
      if (!racedDevice) {
        conflict('远端端口或 FRP 代理名已被其他设备占用')
      }
      device = validateReusableDevice(racedDevice, input)
    }
    device = validateReusableDevice(device, input)
  }

  // The inventory refresh in App is intentionally asynchronous. Use the
  // server's exact registration response as the command source so a stale
  // polling snapshot cannot change the device identity shown to the operator.
  const enrollment = await client.createRuntimeEnrollment(device.id)
  if (enrollment.device_id !== device.id
    || !enrollment.enrollment_token
    || /\s/.test(enrollment.enrollment_token)
    || !Number.isFinite(enrollment.expires_at)) {
    throw new Error('服务端返回了无效或不匹配的 Runtime 配对凭据')
  }
  return { device, enrollment, input }
}
