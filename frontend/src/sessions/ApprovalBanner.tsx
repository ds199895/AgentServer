import { AlertCircle, Check, X } from 'lucide-react'
import { agentApi } from '../agent/api'
import type { AgentSession, AgentRequest } from '../agent/types'

interface ApprovalBannerProps {
  session: AgentSession
  requests: AgentRequest[]
}

export default function ApprovalBanner({ session, requests }: ApprovalBannerProps) {
  if (requests.length === 0) return null

  const request = requests[0] // Show the first pending request

  async function handleRespond(decision: 'approve_once' | 'approve_all' | 'deny') {
    try {
      await agentApi.respond(session.id, request.id, { decision })
    } catch (error) {
      console.error('Failed to respond to approval request:', error)
    }
  }

  return (
    <div className="bg-yellow-50 border-b border-yellow-200 p-4">
      <div className="flex items-start gap-3">
        <AlertCircle className="text-yellow-600 mt-0.5 flex-shrink-0" size={20} />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-yellow-900">{request.title}</div>
          {request.detail && (
            <div className="text-xs text-yellow-700 mt-1">{String(request.detail)}</div>
          )}
          {request.options && request.options.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {request.options.map((opt, idx) => (
                <button
                  key={idx}
                  onClick={() => handleRespond(opt.value as any)}
                  className="px-3 py-1.5 bg-white border border-yellow-300 rounded text-xs font-medium text-yellow-900 hover:bg-yellow-100 transition-colors"
                >
                  {String(opt.label)}
                </button>
              ))}
            </div>
          )}
          {(!request.options || request.options.length === 0) && (
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => handleRespond('approve_once')}
                className="px-3 py-1.5 bg-green-600 text-white rounded text-xs font-medium hover:bg-green-700 transition-colors flex items-center gap-1"
              >
                <Check size={14} />
                Approve Once
              </button>
              <button
                onClick={() => handleRespond('approve_all')}
                className="px-3 py-1.5 bg-green-600 text-white rounded text-xs font-medium hover:bg-green-700 transition-colors flex items-center gap-1"
              >
                <Check size={14} />
                Approve All
              </button>
              <button
                onClick={() => handleRespond('deny')}
                className="px-3 py-1.5 bg-red-600 text-white rounded text-xs font-medium hover:bg-red-700 transition-colors flex items-center gap-1"
              >
                <X size={14} />
                Deny
              </button>
            </div>
          )}
        </div>
      </div>
      {requests.length > 1 && (
        <div className="text-xs text-yellow-600 mt-2">
          +{requests.length - 1} more pending
        </div>
      )}
    </div>
  )
}
