export type TerminalSession = {
  id: string
  name: string
  command: string
  cwd: string
  created_at: number
  active: boolean
  return_code: number | null
  kind: 'local' | 'ssh'
  device_id: string | null
  device_name: string | null
  remote_port: number | null
  services: DetectedService[]
  /** Detected coding agent running in this session; null/absent = plain shell. */
  agent?: {
    kind: string
    cwd: string
    source: string
    since: number
  } | null
  workspace?: {
    kind: 'local' | 'sftp' | string
    root: string
    platform: 'posix' | 'windows' | string
    current_path?: string | null
    binding_id?: string
    available?: boolean
    error?: string
  }
}

export type DetectedService = {
  port: number
  url: string
  label: string
  status: 'checking' | 'online' | 'offline'
  detected_at: number
  last_seen_at: number
  last_checked_at: number | null
  error: string
}

export type Device = {
  id: string
  name: string
  proxy_name: string
  remote_port: number
  ssh_user: string
  remote_shell: 'system' | 'powershell' | 'cmd'
  notes: string
  created_at: number
  updated_at: number
  last_seen_at: number | null
  frp_online: boolean
  ssh_available: boolean
  client_version: string
  last_start_time: string
  last_close_time: string
  last_error: string
  discovered: boolean
  client_id: string
  hostname: string
  client_ip: string
  wire_protocol: string
  first_connected_at: number | null
  /** Device Runtime Host is independent from FRP and SSH availability. */
  runtime?: DeviceRuntimeStatus
}

export type RuntimeProviderCapability = {
  id: string
  transport: string
  available: boolean
  version: string
  features: string[]
  detail_code?: string | null
}

export type DeviceRuntimeStatus = {
  state: 'unregistered' | 'online' | 'degraded' | 'offline' | 'revoked'
  last_seen_at: number | null
  lease_expires_at?: number | null
  host_version: string
  protocol_version: number
  instance_id: string
  boot_id?: string
  generation?: number
  health?: string
  platform?: Record<string, string>
  providers: RuntimeProviderCapability[]
}

export type DeviceRuntimeEnrollment = {
  device_id: string
  enrollment_token: string
  expires_at: number
}

export type RuntimeSession = {
  id: string
  device_id: string
  provider: string
  state: 'requested' | 'starting' | 'ready' | 'running' | 'waiting' | 'stopping' | 'stopped' | 'failed' | 'lost'
  cwd: string
  permission_mode: 'approval-required' | 'workspace-write' | 'full-access' | 'auto'
  model: string | null
  resume_cursor?: Record<string, unknown> | null
  active_turn_id?: string | null
  active_request_id?: string | null
  last_error?: string | null
  revision?: number
  last_event_sequence?: number
  created_at: number
  updated_at: number
}

export type RuntimeEvent = {
  sequence: number
  event_id: string
  device_id: string
  session_id: string | null
  type: string
  turn_id?: string | null
  item_id?: string | null
  interaction_id?: string | null
  payload: Record<string, unknown>
  occurred_at: number | null
  recorded_at: number
}

export type RuntimeRequestOptions = {
  signal?: AbortSignal
}

export function runtimeSessionSocketUrl(sessionId: string, afterSequence = 0): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/runtime-sessions/${encodeURIComponent(sessionId)}?after_sequence=${Math.max(0, afterSequence)}`
}

/** Keep UI state pinned to the device selected by the authenticated owner. */
export function isRuntimeSessionForDevice(session: RuntimeSession, deviceId: string): boolean {
  return Boolean(deviceId) && session.device_id === deviceId
}

/** Reject stale or cross-device event payloads before projecting them into a session view. */
export function isRuntimeEventForSession(
  event: RuntimeEvent,
  deviceId: string,
  sessionId: string,
): boolean {
  return Boolean(deviceId && sessionId)
    && event.device_id === deviceId
    && event.session_id === sessionId
}

export function isAbortError(reason: unknown): boolean {
  return Boolean(reason && typeof reason === 'object' && 'name' in reason && reason.name === 'AbortError')
}

/** Browser-generated idempotency key reused after an ambiguous HTTP failure. */
export function newRuntimeRequestId(): string {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export type RuntimeRequestIdentity = {
  fingerprint: string
  requestId: string
}

export function runtimeRequestIdentityFor(
  current: RuntimeRequestIdentity | null,
  fingerprint: string,
): RuntimeRequestIdentity {
  if (current?.fingerprint === fingerprint) return current
  return { fingerprint, requestId: newRuntimeRequestId() }
}

export type DeviceInput = Pick<Device, 'name' | 'proxy_name' | 'remote_port' | 'ssh_user' | 'remote_shell' | 'notes'> & {
  id?: string
}

export type Preview = {
  id: string
  device_id: string
  device_name: string
  terminal_id: string | null
  target_port: number
  label: string
  created_at: number
  last_access_at: number
  active: boolean
  error: string
  url: string | null
}

export type WorkspaceEntry = {
  name: string
  path: string
  kind: 'file' | 'directory' | 'symlink' | 'other'
  size: number
  modified_at: number
  version: string
  hidden?: boolean
  readonly?: boolean
}

export type WorkspaceBreadcrumb = {
  name: string
  path: string
}

export type WorkspaceListing = {
  workspace_id: string
  path: string
  root: string
  provider: string
  platform: 'posix' | 'windows' | string
  current_path: string | null
  parent: string | null
  parent_path: string | null
  entries: WorkspaceEntry[]
  breadcrumbs: WorkspaceBreadcrumb[]
  revision: string
  next_cursor: string | null
  truncated: boolean
  capabilities: {
    read: boolean
    write: boolean
    watch: boolean
    pagination: boolean
  }
}

export type WorkspaceRequestOptions = {
  cursor?: string | null
  revision?: string | null
  limit?: number
  signal?: AbortSignal
}

export class ApiError extends Error {
  status?: number
  code?: string
}

export type FileGrant = {
  id: string
  terminal_id: string
  name: string
  path: string
  media_type: string
  size: number
  kind: 'file'
  version: string
  etag: string
  preview_mode: 'image' | 'text' | 'pdf' | 'download'
  inline_safe: boolean
  modified_at: number
  expires_at: number
  image_width: number | null
  image_height: number | null
  /** Client-side override used for durable, content-addressed attachments. */
  content_url?: string
  immutable?: boolean
}

export type ArtifactAttachment = {
  id: string
  media_type: string
  size: number
  width: number
  height: number
  name?: string
}

export type ArtifactEvent = {
  sequence?: number
  id: string
  type: string
  event: string
  owner?: string
  terminal_id?: string
  name: string
  path: string
  media_type?: string | null
  size?: number | null
  kind: string
  version: string
  source?: string
  created_at: number
  timestamp: number
  message?: string
  schema_version?: number
  attachment: ArtifactAttachment | null
}

export const frontendBuildSha = __AGENTSERVER_BUILD_SHA__

type TerminalSessionPayload = Omit<TerminalSession, 'services'> & {
  services?: DetectedService[] | null
}

function normalizeTerminalSession(session: TerminalSessionPayload): TerminalSession {
  return {
    ...session,
    services: Array.isArray(session.services) ? session.services : [],
  }
}

/** Apply the same older-backend tolerance to pushed payloads as to HTTP ones. */
export function normalizeTerminalSessions(payload: unknown): TerminalSession[] {
  if (!Array.isArray(payload)) return []
  return payload.map((session) => normalizeTerminalSession(session as TerminalSessionPayload))
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    let code: string | undefined
    try {
      const body = (await response.json()) as {
        detail?: string | { code?: string; message?: string }
      }
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail?.message) message = body.detail.message
      if (typeof body.detail === 'object') code = body.detail?.code
    } catch {
      // Keep the status-based fallback message.
    }
    const error = new ApiError(message)
    error.status = response.status
    error.code = code
    throw error
  }
  return response.json() as Promise<T>
}

export const api = {
  version: () => request<{ build_sha: string }>('/api/version'),
  me: () => request<{ username: string }>('/api/auth/me'),
  login: (username: string, password: string) =>
    request<{ username: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    }),
  terminals: async () => {
    const sessions = await request<TerminalSessionPayload[]>('/api/terminals')
    return sessions.map(normalizeTerminalSession)
  },
  devices: () => request<Device[]>('/api/devices'),
  createDevice: (input: DeviceInput) =>
    request<Device>('/api/devices', { method: 'POST', body: JSON.stringify(input) }),
  updateDevice: (id: string, input: DeviceInput) =>
    request<Device>(`/api/devices/${id}`, { method: 'PUT', body: JSON.stringify(input) }),
  deleteDevice: (id: string) =>
    request<{ ok: boolean }>(`/api/devices/${id}`, { method: 'DELETE' }),
  syncDevices: () => request<{ last_sync_at: number }>('/api/devices/sync', { method: 'POST' }),
  probeDevice: (id: string) =>
    request<{ available: boolean; error: string }>(`/api/devices/${id}/probe`, { method: 'POST' }),
  createRuntimeEnrollment: (id: string, options: RuntimeRequestOptions = {}) =>
    request<DeviceRuntimeEnrollment>(`/api/devices/${encodeURIComponent(id)}/runtime/enrollment-tokens`, {
      method: 'POST',
      body: JSON.stringify({ ttl_seconds: 30 * 60 }),
      cache: 'no-store',
      signal: options.signal,
    }),
  probeDeviceRuntime: (id: string, options: RuntimeRequestOptions = {}) =>
    request<{ command: Record<string, unknown> }>(`/api/devices/${encodeURIComponent(id)}/runtime/probe`, {
      method: 'POST',
      body: '{}',
      signal: options.signal,
    }),
  revokeDeviceRuntime: (id: string, options: RuntimeRequestOptions = {}) =>
    request<{ ok: boolean }>(`/api/devices/${encodeURIComponent(id)}/runtime/credential`, {
      method: 'DELETE',
      signal: options.signal,
    }),
  runtimeSessions: (deviceId: string, options: RuntimeRequestOptions = {}) =>
    request<{ sessions: RuntimeSession[] }>(`/api/devices/${encodeURIComponent(deviceId)}/runtime/sessions`, {
      signal: options.signal,
    }),
  createRuntimeSession: (
    deviceId: string,
    input: {
      session_id?: string
      provider: string
      cwd: string
      permission_mode: RuntimeSession['permission_mode']
      model?: string | null
      resume_cursor?: Record<string, unknown> | null
    },
    options: RuntimeRequestOptions = {},
  ) => request<{ session: RuntimeSession; command: Record<string, unknown> | null }>(
    `/api/devices/${encodeURIComponent(deviceId)}/runtime/sessions`,
    { method: 'POST', body: JSON.stringify(input), signal: options.signal },
  ),
  runtimeSessionEvents: (
    sessionId: string,
    afterSequence = 0,
    options: RuntimeRequestOptions = {},
  ) =>
    request<{ events: RuntimeEvent[] }>(
      `/api/runtime-sessions/${encodeURIComponent(sessionId)}/events?after_sequence=${afterSequence}`,
      { signal: options.signal },
    ),
  startRuntimeTurn: (
    sessionId: string,
    input: string,
    model?: string | null,
    turnId?: string | null,
    options: RuntimeRequestOptions = {},
  ) =>
    request<{ command: Record<string, unknown> }>(
      `/api/runtime-sessions/${encodeURIComponent(sessionId)}/turns`,
      {
        method: 'POST',
        body: JSON.stringify({ input, model: model || null, turn_id: turnId || null }),
        signal: options.signal,
      },
    ),
  interruptRuntimeTurn: (sessionId: string, options: RuntimeRequestOptions = {}) =>
    request<{ command: Record<string, unknown> }>(
      `/api/runtime-sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: 'POST', body: '{}', signal: options.signal },
    ),
  stopRuntimeSession: (sessionId: string, options: RuntimeRequestOptions = {}) =>
    request<{ command: Record<string, unknown> }>(`/api/runtime-sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
      signal: options.signal,
    }),
  respondRuntimeInteraction: (
    sessionId: string,
    interactionId: string,
    response: Record<string, unknown>,
    options: RuntimeRequestOptions = {},
  ) => request<{ command: Record<string, unknown> }>(
    `/api/runtime-sessions/${encodeURIComponent(sessionId)}/interactions/${encodeURIComponent(interactionId)}/respond`,
    { method: 'POST', body: JSON.stringify(response), signal: options.signal },
  ),
  createDeviceTerminal: async (id: string, name?: string, workspaceRoot?: string) => {
    const session = await request<TerminalSessionPayload>(`/api/devices/${id}/terminals`, {
      method: 'POST',
      body: JSON.stringify({ name: name || null, workspace_root: workspaceRoot || null }),
    })
    return normalizeTerminalSession(session)
  },
  createTerminal: async (name?: string, workspaceRoot?: string) => {
    const session = await request<TerminalSessionPayload>('/api/terminals', {
      method: 'POST',
      body: JSON.stringify({ name: name || null, workspace_root: workspaceRoot || null }),
    })
    return normalizeTerminalSession(session)
  },
  deleteTerminal: (id: string) =>
    request<{ ok: boolean }>(`/api/terminals/${id}`, { method: 'DELETE' }),
  workspace: (id: string, path = '', options: WorkspaceRequestOptions = {}) => {
    const query = new URLSearchParams()
    query.set('path', path)
    if (options.cursor) query.set('cursor', options.cursor)
    if (options.revision) query.set('revision', options.revision)
    if (options.limit) query.set('limit', String(options.limit))
    return request<WorkspaceListing>(
      `/api/terminals/${encodeURIComponent(id)}/workspace?${query}`,
      { signal: options.signal },
    )
  },
  resolveFile: (id: string, path: string) =>
    request<FileGrant>(`/api/terminals/${encodeURIComponent(id)}/files/resolve`, {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  fileContentUrl: (grant: Pick<FileGrant, 'id' | 'terminal_id' | 'content_url'>) => {
    if (grant.content_url) return grant.content_url
    const query = new URLSearchParams({ terminal_id: grant.terminal_id })
    return `/api/files/${encodeURIComponent(grant.id)}/content?${query}`
  },
  attachmentContentUrl: (terminalId: string, attachmentId: string) =>
    `/api/terminals/${encodeURIComponent(terminalId)}/attachments/${encodeURIComponent(attachmentId)}`,
  artifacts: (id: string) =>
    request<ArtifactEvent[]>(`/api/terminals/${encodeURIComponent(id)}/artifacts`),
  previews: () => request<Preview[]>('/api/previews'),
  createPreview: (deviceId: string, port: number, label?: string, terminalId?: string) =>
    request<Preview>(`/api/devices/${deviceId}/previews`, {
      method: 'POST',
      body: JSON.stringify({ port, label: label || null, terminal_id: terminalId || null }),
    }),
  previewTicket: (id: string) =>
    request<{ url: string }>(`/api/previews/${id}/ticket`, { method: 'POST' }),
  deletePreview: (id: string) =>
    request<{ ok: boolean }>(`/api/previews/${id}`, { method: 'DELETE' }),
}
