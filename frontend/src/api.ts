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
  terminals: () => request<TerminalSession[]>('/api/terminals'),
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
  createDeviceTerminal: (id: string, name?: string) =>
    request<TerminalSession>(`/api/devices/${id}/terminals`, {
      method: 'POST',
      body: JSON.stringify({ name: name || null }),
    }),
  createTerminal: (name?: string) =>
    request<TerminalSession>('/api/terminals', {
      method: 'POST',
      body: JSON.stringify({ name: name || null }),
    }),
  deleteTerminal: (id: string) =>
    request<{ ok: boolean }>(`/api/terminals/${id}`, { method: 'DELETE' }),
}
