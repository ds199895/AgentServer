export type AgentMessage = {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system' | 'reasoning'
  text: string
  turn_id?: string | null
  item_id?: string | null
  created_at: number
  streaming?: boolean
  sequence?: number
}

export type AgentActivity = {
  id: string
  session_id: string
  kind: 'plan' | 'tool' | 'file' | 'command' | 'output' | 'status' | string
  title: string
  status: string
  detail?: string
  input?: unknown
  output?: unknown
  turn_id?: string | null
  item_id?: string | null
  created_at: number
  updated_at: number
  collapsed?: boolean
  sequence?: number
}

export type AgentRequest = {
  id: string
  session_id: string
  kind: 'approval' | 'user_input'
  title: string
  detail: string
  options: Array<Record<string, unknown>>
  status: 'pending' | 'resolved'
  turn_id?: string | null
  created_at: number
  input?: unknown
  response?: unknown
  resolved_at?: number | null
  sequence?: number
}

export type AgentTurn = {
  id: string
  session_id: string
  input: string
  state: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted'
  created_at: number
  completed_at?: number | null
  error?: string | null
}

export type AgentSession = {
  id: string
  owner_id?: string
  device_id: string | null
  provider: string
  cwd: string
  permission_mode: string
  model: string | null
  state: 'starting' | 'ready' | 'running' | 'waiting' | 'disconnected' | 'stopping' | 'stopped' | 'failed'
  session_kind: 'agent'
  created_at: number
  updated_at: number
  active_turn_id: string | null
  last_error: string | null
  resume_cursor?: Record<string, unknown> | null
  sequence: number
  executor_id: string
  bridge_instance_id: string
  transport: string
  device_generation: number
  platform: Record<string, unknown>
  capabilities: Record<string, unknown>
  connector_sequence: number
  messages: AgentMessage[]
  activities: AgentActivity[]
  requests: AgentRequest[]
  turns: AgentTurn[]
}

export type AgentEvent = {
  sequence: number
  event_id: string
  session_id: string
  type: string
  payload: Record<string, unknown>
  occurred_at: number
}
