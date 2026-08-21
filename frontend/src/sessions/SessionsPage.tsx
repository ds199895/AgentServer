import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Plus, Search, Trash2, Bot } from 'lucide-react'
import { api, type Device } from '../api'
import { agentApi } from '../agent/api'
import { AgentStartDialog } from '../agent/AgentStartDialog'
import { useAgentSession } from '../agent/useAgentSession'
import SessionTimeline from './SessionTimeline'
import SessionComposer from './SessionComposer'
import type { AgentSession } from '../agent/types'

/** Sessions are listed without history, so only metadata fields are present. */
type SessionSummary = Pick<
  AgentSession,
  'id' | 'provider' | 'device_id' | 'cwd' | 'state' | 'created_at' | 'updated_at'
>

const STATE_STYLES: Record<string, string> = {
  ready: 'text-primary',
  running: 'text-warning',
  waiting: 'text-warning',
  failed: 'text-destructive',
  stopped: 'text-muted-foreground',
  disconnected: 'text-destructive',
}

export default function SessionsPage() {
  const { sessionId } = useParams<{ sessionId?: string }>()
  const navigate = useNavigate()
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [devices, setDevices] = useState<Device[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [startDevice, setStartDevice] = useState<Device | null>(null)
  const [listError, setListError] = useState('')
  const { session, error, send } = useAgentSession(sessionId ?? null)

  const refreshSessions = useCallback(async () => {
    try {
      const result = await agentApi.sessions()
      setSessions(result.sessions as SessionSummary[])
      setListError('')
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : 'Unable to load sessions')
    }
  }, [])

  useEffect(() => {
    void refreshSessions()
    void api.devices().then(setDevices).catch(() => setDevices([]))
  }, [refreshSessions])

  // The open session owns its own live projection; refreshing the list when its
  // state changes keeps the sidebar badge in step without polling.
  useEffect(() => {
    if (session) void refreshSessions()
  }, [session?.state, refreshSessions])

  const query = searchQuery.trim().toLowerCase()
  const filteredSessions = query
    ? sessions.filter((item) =>
        [item.id, item.provider, item.cwd, item.device_id ?? '']
          .join(' ')
          .toLowerCase()
          .includes(query),
      )
    : sessions

  async function handleDelete(id: string, event: React.MouseEvent) {
    event.stopPropagation()
    if (!window.confirm('Close this agent session? Its transcript is kept, but the provider process stops.')) {
      return
    }
    try {
      await agentApi.close(id)
      await refreshSessions()
      if (id === sessionId) navigate('/sessions')
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : 'Unable to close session')
    }
  }

  // Devices that actually have an agent bridge online — starting a session on
  // anything else fails server-side, so they are not offered.
  const startableDevices = devices.filter((device) => device.runtime?.state === 'online')

  return (
    <div className="flex h-full">
      <aside className="flex w-72 flex-col border-r border-border bg-secondary">
        <div className="border-b border-border p-3">
          <button
            onClick={() => setStartDevice(startableDevices[0] ?? null)}
            disabled={startableDevices.length === 0}
            title={
              startableDevices.length === 0
                ? 'No device has an agent runtime online'
                : 'Start a new agent session'
            }
            className="flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Plus size={16} />
            New Session
          </button>
        </div>

        <div className="border-b border-border p-3">
          <div className="relative">
            <Search
              className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
              size={15}
            />
            <input
              type="search"
              placeholder="Search sessions…"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              className="w-full rounded-md border border-input bg-background py-2 pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
        </div>

        {listError && (
          <div className="border-b border-border px-3 py-2 text-xs text-destructive">{listError}</div>
        )}

        <div className="flex-1 overflow-y-auto">
          {filteredSessions.length === 0 && (
            <div className="p-4 text-center text-sm text-muted-foreground">
              {sessions.length === 0 ? 'No sessions yet' : 'No matching sessions'}
            </div>
          )}
          {filteredSessions.map((item) => (
            <div
              key={item.id}
              role="button"
              tabIndex={0}
              onClick={() => navigate(`/sessions/${item.id}`)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  navigate(`/sessions/${item.id}`)
                }
              }}
              className={`group flex w-full cursor-pointer items-start gap-2 border-b border-border px-3 py-2.5 text-left transition-colors hover:bg-card ${
                item.id === sessionId ? 'border-l-2 border-l-primary bg-card' : ''
              }`}
            >
              <Bot size={15} className="mt-0.5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-foreground">
                  {item.cwd || item.id}
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <span className={STATE_STYLES[item.state] ?? 'text-muted-foreground'}>
                    {item.state}
                  </span>
                  <span>·</span>
                  <span className="truncate">{item.provider}</span>
                </div>
              </div>
              <button
                type="button"
                onClick={(event) => void handleDelete(item.id, event)}
                title="Close session"
                aria-label={`Close session ${item.id}`}
                className="shrink-0 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus:opacity-100 group-hover:opacity-100"
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        {!sessionId && (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            Select a session, or start a new one.
          </div>
        )}
        {sessionId && !session && !error && (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            Loading session…
          </div>
        )}
        {sessionId && error && !session && (
          <div className="flex flex-1 items-center justify-center text-sm text-destructive">
            {error}
          </div>
        )}
        {session && (
          <>
            <header className="flex items-center gap-3 border-b border-border px-5 py-3">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-foreground">{session.cwd}</div>
                <div className="truncate font-mono text-xs text-muted-foreground">
                  {session.provider}
                  {session.device_id ? ` · ${session.device_id}` : ''}
                  {session.model ? ` · ${session.model}` : ''}
                </div>
              </div>
              <span
                className={`shrink-0 rounded-full border border-border px-2 py-1 text-xs ${
                  STATE_STYLES[session.state] ?? 'text-muted-foreground'
                }`}
              >
                {session.state}
              </span>
            </header>
            {/* Live connection errors surface above the transcript rather than
                replacing it, so history stays readable while reconnecting. */}
            {error && (
              <div className="border-b border-border bg-destructive/10 px-5 py-2 text-xs text-destructive">
                {error}
              </div>
            )}
            <SessionTimeline session={session} />
            <SessionComposer session={session} onSend={send} />
          </>
        )}
      </section>

      {startDevice && (
        <AgentStartDialog
          device={startDevice}
          onClose={() => setStartDevice(null)}
          onStart={async (options) => {
            const created = await agentApi.create({ ...options, device_id: startDevice.id })
            await refreshSessions()
            navigate(`/sessions/${created.session.id}`)
          }}
        />
      )}
    </div>
  )
}
