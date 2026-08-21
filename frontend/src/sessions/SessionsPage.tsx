import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Plus, Search } from 'lucide-react'
import { agentApi } from '../agent/api'
import { useAgentSession } from '../agent/useAgentSession'
import SessionTimeline from './SessionTimeline'
import SessionComposer from './SessionComposer'
import type { AgentSession } from '../agent/types'

export default function SessionsPage() {
  const { sessionId } = useParams<{ sessionId?: string }>()
  const navigate = useNavigate()
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const { session, error, send } = useAgentSession(sessionId ?? null)

  useEffect(() => {
    async function loadSessions() {
      const result = await agentApi.sessions()
      setSessions(result.sessions)
    }
    void loadSessions()
  }, [])

  const filteredSessions = sessions.filter((s) =>
    s.id.toLowerCase().includes(searchQuery.toLowerCase())
  )

  async function handleNewSession() {
    const result = await agentApi.create({
      provider: 'generic',
      device_id: 'local',
      cwd: '.',
      permission_mode: 'workspace-write',
      model: null,
    })
    navigate(`/sessions/${result.session.id}`)
  }

  return (
    <div className="flex h-full">
      {/* Session List Sidebar */}
      <div className="w-64 border-r border-gray-200 flex flex-col bg-gray-50">
        <div className="p-4 border-b border-gray-200">
          <button
            onClick={handleNewSession}
            className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors"
          >
            <Plus size={16} />
            New Session
          </button>
        </div>

        <div className="p-4 border-b border-gray-200">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={16} />
            <input
              type="text"
              placeholder="Search sessions..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {filteredSessions.length === 0 && (
            <div className="p-4 text-sm text-gray-500 text-center">
              {sessions.length === 0 ? 'No sessions yet' : 'No matching sessions'}
            </div>
          )}
          {filteredSessions.map((s) => (
            <button
              key={s.id}
              onClick={() => navigate(`/sessions/${s.id}`)}
              className={`w-full px-4 py-3 text-left border-b border-gray-200 hover:bg-white transition-colors ${
                s.id === sessionId ? 'bg-white border-l-4 border-l-blue-600' : ''
              }`}
            >
              <div className="text-sm font-medium text-gray-900 truncate">{s.id}</div>
              <div className="text-xs text-gray-500 mt-1">
                {s.state} · {new Date(s.created_at * 1000).toLocaleDateString()}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col">
        {!sessionId && (
          <div className="flex-1 flex items-center justify-center text-gray-500">
            Select a session or create a new one
          </div>
        )}
        {sessionId && !session && !error && (
          <div className="flex-1 flex items-center justify-center text-gray-500">
            Loading session...
          </div>
        )}
        {error && (
          <div className="flex-1 flex items-center justify-center text-red-600">
            {error}
          </div>
        )}
        {session && (
          <>
            <div className="flex-1 overflow-hidden flex flex-col">
              <SessionTimeline session={session} />
            </div>
            <SessionComposer session={session} onSend={send} />
          </>
        )}
      </div>
    </div>
  )
}
