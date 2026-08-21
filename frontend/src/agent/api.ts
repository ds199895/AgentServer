import type { AgentSession } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `Request failed (${response.status})`)
  return response.json() as Promise<T>
}

export const agentApi = {
  sessions: () => request<{ sessions: AgentSession[] }>('/api/agent/sessions'),
  create: (body: Pick<AgentSession, 'provider' | 'device_id' | 'cwd' | 'permission_mode' | 'model'> & { session_id?: string }) => request<{ session: AgentSession }>('/api/agent/sessions', { method: 'POST', body: JSON.stringify(body) }),
  get: (id: string) => request<{ session: AgentSession }>(`/api/agent/sessions/${encodeURIComponent(id)}`),
  turn: (id: string, input: string, turnId?: string, options?: { model?: string | null; effort?: string | null }) => request<{ turn: AgentSession['turns'][number] }>(`/api/agent/sessions/${encodeURIComponent(id)}/turns`, { method: 'POST', body: JSON.stringify({ input, turn_id: turnId, model: options?.model ?? null, effort: options?.effort ?? null }) }),
  interrupt: (id: string) => request<{ accepted: boolean }>(`/api/agent/sessions/${encodeURIComponent(id)}/interrupt`, { method: 'POST', body: '{}' }),
  respond: (id: string, requestId: string, payload: Record<string, unknown>) => request<{ accepted: boolean }>(`/api/agent/sessions/${encodeURIComponent(id)}/requests/respond`, { method: 'POST', body: JSON.stringify({ request_id: requestId, payload }) }),
  browse: (deviceId: string, path?: string | null) => request<{ path: string; parent: string | null; entries: Array<{ name: string; path: string }>; truncated: boolean }>(`/api/agent/devices/${encodeURIComponent(deviceId)}/browse`, { method: 'POST', body: JSON.stringify({ path: path ?? null }) }),
  close: (id: string) => request<{ accepted: boolean }>(`/api/agent/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}

export function newAgentTurnId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `turn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

export function agentSessionSocketUrl(id: string, afterSequence = 0): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/agent/sessions/${encodeURIComponent(id)}?after_sequence=${Math.max(0, afterSequence)}`
}
