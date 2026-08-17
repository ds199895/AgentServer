import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from '@/api'
import {
  isExecutionUnavailableStatus,
  shouldReconnectExecutionSocket,
} from '@/execution-contract'
import {
  executionSocketUrl,
  fetchExecutionSnapshot,
} from '@/execution-api'
import {
  applyExecutionMessage,
  createSingleFlight,
  nextEvidenceExpiry,
  type ExecutionSnapshot,
  type ExecutionStreamMessage,
} from '@/execution-state'

export type ExecutionConnectionStatus =
  | 'disabled'
  | 'loading'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'resyncing'
  | 'degraded'
  | 'unavailable'
  | 'unauthorized'

export type ExecutionStreamState = {
  snapshot: ExecutionSnapshot | null
  status: ExecutionConnectionStatus
  error: string
  available: boolean
  freshness_now: number
  refresh: () => void
}

function apiStatus(error: unknown): number | undefined {
  return error instanceof ApiError ? error.status : undefined
}

export function useExecutionStream(enabled: boolean): ExecutionStreamState {
  const [snapshot, setSnapshot] = useState<ExecutionSnapshot | null>(null)
  const [status, setStatus] = useState<ExecutionConnectionStatus>('disabled')
  const [error, setError] = useState('')
  const [freshnessNow, setFreshnessNow] = useState(() => Date.now())
  const [refreshGeneration, setRefreshGeneration] = useState(0)
  const snapshotRef = useRef<ExecutionSnapshot | null>(null)

  const refresh = useCallback(() => setRefreshGeneration((current) => current + 1), [])

  useEffect(() => {
    if (!enabled) {
      snapshotRef.current = null
      setSnapshot(null)
      setStatus('disabled')
      setError('')
      setFreshnessNow(Date.now())
      return
    }

    let disposed = false
    let stopped = false
    let resyncing = false
    let socket: WebSocket | null = null
    let socketGeneration = 0
    let reconnectAttempt = 0
    let reconnectTimer: number | undefined
    let snapshotRetryTimer: number | undefined
    let requestController: AbortController | null = null
    const singleSnapshotRequest = createSingleFlight<void>()

    const publishSnapshot = (next: ExecutionSnapshot | null) => {
      snapshotRef.current = next
      setSnapshot(next)
      setFreshnessNow(Date.now())
    }

    const closeSocket = () => {
      socketGeneration += 1
      const previous = socket
      socket = null
      if (!previous) return
      previous.onopen = null
      previous.onmessage = null
      previous.onerror = null
      previous.onclose = null
      if (previous.readyState === WebSocket.CONNECTING || previous.readyState === WebSocket.OPEN) {
        previous.close(1000)
      }
    }

    const retryDelay = () => Math.min(750 * 2 ** reconnectAttempt, 8_000)

    let connect: () => void
    let synchronize: () => Promise<void>

    const scheduleConnect = () => {
      if (disposed || stopped || resyncing || reconnectTimer !== undefined) return
      const delay = retryDelay()
      reconnectAttempt += 1
      setStatus('reconnecting')
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined
        connect()
      }, delay)
    }

    const scheduleSnapshotRetry = () => {
      if (disposed || stopped || snapshotRetryTimer !== undefined) return
      const delay = retryDelay()
      reconnectAttempt += 1
      snapshotRetryTimer = window.setTimeout(() => {
        snapshotRetryTimer = undefined
        void synchronize()
      }, delay)
    }

    connect = () => {
      if (disposed || stopped || resyncing || !snapshotRef.current) return
      closeSocket()
      const generation = socketGeneration
      const nextSocket = new WebSocket(
        executionSocketUrl(snapshotRef.current.as_of_sequence),
      )
      socket = nextSocket
      setStatus('connecting')
      nextSocket.onopen = () => {
        if (disposed || generation !== socketGeneration) return
        reconnectAttempt = 0
        setError('')
        setStatus('live')
      }
      nextSocket.onmessage = (message) => {
        if (disposed || generation !== socketGeneration) return
        let payload: ExecutionStreamMessage
        try {
          payload = JSON.parse(String(message.data)) as ExecutionStreamMessage
        } catch {
          return
        }
        if (payload.type === 'resync_required') {
          void synchronize()
          return
        }
        if (payload.type !== 'event') return
        const current = snapshotRef.current
        if (!current) {
          void synchronize()
          return
        }
        const next = applyExecutionMessage(current, payload)
        if (!next) {
          void synchronize()
          return
        }
        if (next !== current) publishSnapshot(next)
      }
      nextSocket.onerror = () => {
        if (generation === socketGeneration) nextSocket.close()
      }
      nextSocket.onclose = (event) => {
        if (disposed || generation !== socketGeneration) return
        socket = null
        if (!shouldReconnectExecutionSocket(event.code)) {
          stopped = true
          setStatus('unauthorized')
          setError('Execution 状态连接已失去授权')
          return
        }
        scheduleConnect()
      }
    }

    synchronize = () => singleSnapshotRequest(async () => {
      if (disposed || stopped) return
      resyncing = snapshotRef.current !== null
      window.clearTimeout(reconnectTimer)
      reconnectTimer = undefined
      closeSocket()
      requestController?.abort()
      requestController = new AbortController()
      setStatus(resyncing ? 'resyncing' : 'loading')
      try {
        const next = await fetchExecutionSnapshot(requestController.signal)
        if (disposed) return
        publishSnapshot(next)
        reconnectAttempt = 0
        resyncing = false
        setError('')
        connect()
      } catch (reason) {
        if (disposed || (reason instanceof DOMException && reason.name === 'AbortError')) return
        const responseStatus = apiStatus(reason)
        resyncing = false
        if (isExecutionUnavailableStatus(responseStatus)) {
          stopped = true
          publishSnapshot(null)
          setError('')
          setStatus('unavailable')
          return
        }
        if (responseStatus === 401) {
          stopped = true
          publishSnapshot(null)
          setStatus('unauthorized')
          setError('Execution 状态接口未授权')
          return
        }
        setStatus('degraded')
        setError(reason instanceof Error ? reason.message : 'Execution 状态暂时不可用')
        scheduleSnapshotRetry()
      }
    })

    void synchronize()
    return () => {
      disposed = true
      requestController?.abort()
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(snapshotRetryTimer)
      closeSocket()
    }
  }, [enabled, refreshGeneration])

  useEffect(() => {
    const expiresAt = nextEvidenceExpiry(snapshot, freshnessNow)
    if (expiresAt === null) return
    const delay = Math.min(
      2_147_483_647,
      Math.max(1, expiresAt - Date.now() + 1),
    )
    const timer = window.setTimeout(() => setFreshnessNow(Date.now()), delay)
    return () => window.clearTimeout(timer)
  }, [freshnessNow, snapshot])

  return useMemo(() => ({
    snapshot,
    status,
    error,
    available: status !== 'disabled' && status !== 'unavailable' && status !== 'unauthorized',
    freshness_now: freshnessNow,
    refresh,
  }), [error, freshnessNow, refresh, snapshot, status])
}
