import { useCallback, useEffect, useState } from 'react'
import { agentApi, agentSessionSocketUrl } from './api'
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
    const connect = async () => {
      try {
        const value = await agentApi.get(sessionId)
        if (disposed) return
        setSession(value.session)
        socket = new WebSocket(agentSessionSocketUrl(sessionId, value.session.sequence))
        socket.onmessage = async (message) => {
          const event = JSON.parse(message.data) as AgentEvent
          if (!disposed) await reload()
          void event
        }
        socket.onerror = () => { if (!disposed) setError('Agent session connection lost') }
      } catch (reason) { if (!disposed) setError(reason instanceof Error ? reason.message : 'Unable to load agent session') }
    }
    void connect()
    return () => { disposed = true; socket?.close() }
  }, [reload, sessionId])
  return { session, error, reload, send: async (input: string) => { if (!sessionId) return; await agentApi.turn(sessionId, input); await reload() } }
}
