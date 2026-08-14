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
  workspace?: {
    kind: 'local' | 'sftp' | string
    root: string
    platform: 'posix' | 'windows' | string
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
}

export type WorkspaceBreadcrumb = {
  name: string
  path: string
}

export type WorkspaceListing = {
  path: string
  root: string
  provider: string
  parent: string | null
  parent_path: string | null
  entries: WorkspaceEntry[]
  breadcrumbs: WorkspaceBreadcrumb[]
  truncated: boolean
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) message = body.detail
    } catch {
      // Keep the status-based fallback message.
    }
    const error = new Error(message) as Error & { status?: number }
    error.status = response.status
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
  workspace: (id: string, path = '') => {
    const query = new URLSearchParams()
    query.set('path', path)
    return request<WorkspaceListing>(`/api/terminals/${encodeURIComponent(id)}/workspace?${query}`)
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
