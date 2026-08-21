import { useCallback, useEffect, useState } from 'react'
import { agentApi, agentSessionSocketUrl, newAgentTurnId } from './api'
import { applyAgentEvent, hasSequenceGap, isAlreadyApplied } from './reducer'
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
    // Set while a gap repair is in flight so the events arriving behind it are
    // dropped instead of being folded onto a projection they no longer follow.
    let repairing = false

    const repair = async () => {
      repairing = true
      try {
        const value = await agentApi.get(sessionId)
        if (disposed) return
        setSession(value.session)
        cursor = Math.max(cursor, value.session.sequence)
      } finally {
        repairing = false
      }
    }

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
        socket.onmessage = (message) => {
          if (disposed || repairing) return
          const event = JSON.parse(message.data) as AgentEvent
          cursor = Math.max(cursor, event.sequence)
          setSession((current) => {
            if (!current) return current
            // Replay after a reconnect re-sends events already projected.
            if (isAlreadyApplied(current, event)) return current
            if (hasSequenceGap(current, event)) { void repair(); return current }
            return applyAgentEvent(current, event)
          })
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
  return {
    session,
    error,
    reload,
    // The queued turn and its echoed user message both arrive over the socket,
    // so sending only has to hand the input to the server.
    send: async (input: string) => {
      if (!sessionId) return
      await agentApi.turn(sessionId, input, newAgentTurnId())
    },
  }
}
