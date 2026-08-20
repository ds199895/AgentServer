import { useCallback, useEffect, useState } from 'react'
import { agentApi, agentSessionSocketUrl, newAgentTurnId } from './api'
import type { AgentEvent, AgentSession } from './types'

export function useAgentSession(sessionId: string | null) {
  const [session, setSession] = useState<AgentSession | null>(null)
  const [error, setError] = useState('')
  const reload = useCallback(async () => {
    if (!sessionId) return
    const value = await agentApi.get(sessionId)
    setSession(value.session)
  }, [sessionId])
  useEffect(() => {
    if (!sessionId) { setSession(null); return }
    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let reconnectAttempt = 0
    let cursor = 0
    const connect = async () => {
      try {
        const value = await agentApi.get(sessionId)
        if (disposed) return
        setSession(value.session)
        cursor = Math.max(cursor, value.session.sequence)
        socket = new WebSocket(agentSessionSocketUrl(sessionId, cursor))
        socket.onopen = () => {
          reconnectAttempt = 0
          if (!disposed) setError('')
        }
        socket.onmessage = async (message) => {
          const event = JSON.parse(message.data) as AgentEvent
          cursor = Math.max(cursor, event.sequence)
          if (!disposed) await reload()
        }
        socket.onerror = () => {
          if (!disposed) setError('Agent session connection lost; reconnecting…')
          socket?.close()
        }
        socket.onclose = () => {
          if (disposed) return
          const delay = Math.min(10_000, 250 * 2 ** reconnectAttempt++)
          reconnectTimer = window.setTimeout(() => void connect(), delay)
        }
      } catch (reason) { if (!disposed) setError(reason instanceof Error ? reason.message : 'Unable to load agent session') }
    }
    void connect()
    return () => {
      disposed = true
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [reload, sessionId])
  return { session, error, reload, send: async (input: string) => { if (!sessionId) return; await agentApi.turn(sessionId, input, newAgentTurnId()); await reload() } }
}
