import { ApiError } from '@/api'
import { executionSocketPath } from '@/execution-contract'
import {
  normalizeExecutionEventsPage,
  type ExecutionEventsPage,
} from '@/execution-events'
import {
  normalizeExecutionSnapshot,
  type ExecutionSnapshot,
} from '@/execution-state'

export type { ExecutionEventsPage } from '@/execution-events'

async function executionRequest(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    let code: string | undefined
    try {
      const body = await response.json() as {
        detail?: string | { code?: string; message?: string }
      }
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail?.message) message = body.detail.message
      if (typeof body.detail === 'object') code = body.detail?.code
    } catch {
      // Keep the status-based fallback for non-JSON proxy responses.
    }
    const error = new ApiError(message)
    error.status = response.status
    error.code = code
    throw error
  }
  return response.json()
}

export function executionSocketUrl(
  afterSequence: number,
  location: Pick<Location, 'protocol' | 'host'> = window.location,
): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}${executionSocketPath(afterSequence)}`
}

export async function fetchExecutionSnapshot(signal?: AbortSignal): Promise<ExecutionSnapshot> {
  return normalizeExecutionSnapshot(await executionRequest('/api/execution/snapshot', signal))
}

export async function fetchRunEvents(
  runId: string,
  options: { afterSequence?: number; limit?: number; signal?: AbortSignal } = {},
): Promise<ExecutionEventsPage> {
  const afterSequence = Math.max(0, Math.floor(options.afterSequence ?? 0))
  const limit = Math.min(1000, Math.max(1, Math.floor(options.limit ?? 200)))
  const query = new URLSearchParams()
  query.set('after_sequence', String(afterSequence))
  query.set('limit', String(limit))
  const payload = await executionRequest(
    `/api/runs/${encodeURIComponent(runId)}/events?${query}`,
    options.signal,
  )
  return normalizeExecutionEventsPage(payload, { afterSequence, limit })
}
