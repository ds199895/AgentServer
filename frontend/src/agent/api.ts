import type { AgentEvent, AgentSession } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `Request failed (${response.status})`)
  return response.json() as Promise<T>
}

export const agentApi = {
  sessions: () => request<{ sessions: AgentSession[] }>('/api/agent/sessions'),
  create: (body: Pick<AgentSession, 'provider' | 'device_id' | 'cwd' | 'permission_mode' | 'model'> & { session_id?: string }) => request<{ session: AgentSession }>('/api/agent/sessions', { method: 'POST', body: JSON.stringify(body) }),
  get: (id: string) => request<{ session: AgentSession }>(`/api/agent/sessions/${encodeURIComponent(id)}`),
  turn: (id: string, input: string) => request<{ turn: AgentSession['turns'][number] }>(`/api/agent/sessions/${encodeURIComponent(id)}/turns`, { method: 'POST', body: JSON.stringify({ input }) }),
  interrupt: (id: string) => request<{ accepted: boolean }>(`/api/agent/sessions/${encodeURIComponent(id)}/interrupt`, { method: 'POST', body: '{}' }),
  respond: (id: string, requestId: string, payload: Record<string, unknown>) => request<{ accepted: boolean }>(`/api/agent/sessions/${encodeURIComponent(id)}/requests/respond`, { method: 'POST', body: JSON.stringify({ request_id: requestId, payload }) }),
}

export function agentSessionSocketUrl(id: string, afterSequence = 0): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/agent/sessions/${encodeURIComponent(id)}?after_sequence=${Math.max(0, afterSequence)}`
}

export function applyAgentEvent(session: AgentSession, value: AgentEvent): AgentSession {
  // Server snapshots are authoritative. Refreshing after a sequence gap also
  // keeps reconnect handling deterministic when an event queue overflows.
  return { ...session, sequence: Math.max(session.sequence, value.sequence) }
}
