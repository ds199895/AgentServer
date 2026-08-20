export type RunLifecycle =
  | 'pending'
  | 'starting'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'lost'

export type RunActivity =
  | 'idle'
  | 'thinking'
  | 'planning'
  | 'coding'
  | 'tooling'
  | 'testing'
  | 'reviewing'
  | 'waiting'
  | 'finalizing'
  | 'unknown'

export type WaitReason =
  | 'user_input'
  | 'approval'
  | 'authentication'
  | 'tool'
  | 'child_run'
  | 'network'
  | 'rate_limit'
  | 'retry_backoff'
  | 'dependency'
  | 'resource'
  | 'unknown'

export type EvidenceMode = 'reported' | 'adapter' | 'observed' | 'inferred' | 'control' | 'stale'
export type EvidenceFreshness = 'fresh' | 'stale' | 'unknown'
export type ExecutionTimestamp = number | string

/** Evidence is deliberately attached per field: cwd, activity and outcome have
 * different authorities and expiry rules, so a single source on the whole Run
 * would make a passive observation look authoritative for unrelated fields. */
export type ExecutionFieldEvidence = {
  source: EvidenceMode
  confidence?: number | null
  observed_at?: ExecutionTimestamp | null
  recorded_at?: ExecutionTimestamp | null
  expires_at?: ExecutionTimestamp | null
  valid_for_ms?: number | null
  fresh?: boolean
  stale?: boolean
  producer_id?: string | null
  global_sequence?: number
}

export type ExecutionEvidence = Record<string, ExecutionFieldEvidence | undefined>

export type ExecutionScope = {
  owner_id?: string | null
  device_id?: string | null
  terminal_id?: string | null
  launch_id?: string | null
  agent_instance_id?: string | null
  task_id?: string | null
  assignment_id?: string | null
  run_id?: string | null
  parent_run_id?: string | null
  span_id?: string | null
}

export type ExecutionProducer = {
  id: string
  epoch: string
  seq: number
  adapter?: string | null
  version?: string | null
  mode: 'active' | 'adapter' | 'observed' | 'system' | 'control'
}

export type ExecutionEvent = {
  schema: 'agentserver.event/1' | string
  event_id: string
  global_sequence: number
  stream_version: number | null
  type: string
  scope: ExecutionScope
  producer: ExecutionProducer
  occurred_at: ExecutionTimestamp
  recorded_at: ExecutionTimestamp
  causation_id?: string | null
  correlation_id?: string | null
  traceparent?: string | null
  evidence?: ExecutionFieldEvidence | null
  payload: Record<string, unknown>
  normalized_state?: ExecutionProjectionDelta | null
}

export type ExecutionRun = {
  run_id: string
  owner_id: string
  task_id: string
  assignment_id: string
  parent_run_id?: string | null
  terminal_id?: string | null
  launch_id?: string | null
  device_id?: string | null
  agent_instance_id?: string | null
  agent_kind?: string | null
  lifecycle: RunLifecycle
  activity?: RunActivity | null
  wait_reason?: WaitReason | null
  wait_target_run_id?: string | null
  summary?: string | null
  progress?: number | null
  stale?: boolean
  attempt?: number
  revision: number
  /** Monotonic materialized-view generation. Unlike aggregate revision this
   * also advances when passive field evidence changes. */
  view_sequence: number
  last_sequence?: number
  created_at?: ExecutionTimestamp
  updated_at: ExecutionTimestamp
  evidence?: ExecutionEvidence
}

export type ExecutionTask = {
  task_id: string
  owner_id: string
  context_id?: string | null
  title: string
  status: string
  revision: number
  view_sequence: number
  created_at: ExecutionTimestamp
  updated_at: ExecutionTimestamp
}

export type ExecutionAssignment = {
  assignment_id: string
  owner_id: string
  task_id: string
  terminal_id?: string | null
  device_id?: string | null
  agent_instance_id?: string | null
  status: string
  lease_expires_at?: ExecutionTimestamp | null
  revision: number
  view_sequence: number
  updated_at?: ExecutionTimestamp
}

export type ExecutionAgentInstance = {
  agent_instance_id: string
  owner_id: string
  device_id?: string | null
  terminal_id?: string | null
  launch_id?: string | null
  kind?: string | null
  lifecycle: string
  stale?: boolean
  cwd?: string | null
  capabilities?: string[]
  last_seen_at?: ExecutionTimestamp | null
  revision: number
  view_sequence: number
  updated_at?: ExecutionTimestamp
  evidence?: ExecutionEvidence
}

/** The server is authoritative for which Run is foreground on a terminal.
 * Clients must never compare revisions belonging to two different Runs. */
export type ExecutionTerminalBinding = {
  terminal_id: string
  active_run_id?: string | null
  active_agent_instance_id?: string | null
  revision: number
  view_sequence: number
  updated_at?: ExecutionTimestamp
}

export type ExecutionRelation = {
  relation_id: string
  owner_id?: string
  relation: string
  source_kind: string
  source_id: string
  target_kind: string
  target_id: string
  attributes?: Record<string, unknown>
  created_at?: ExecutionTimestamp
  revision: number
  view_sequence: number
}

export type ExecutionSnapshot = {
  schema: 'agentserver.execution-snapshot/1' | string
  owner_id?: string
  as_of_sequence: number
  tasks: ExecutionTask[]
  assignments: ExecutionAssignment[]
  runs: ExecutionRun[]
  agents: ExecutionAgentInstance[]
  terminal_bindings: ExecutionTerminalBinding[]
  /** Undefined means an older server omitted the relation view, in which case
   * RunTree falls back to the legacy run.parent_run_id attribute. An empty
   * array is authoritative and deliberately distinct from undefined. */
  relations?: ExecutionRelation[]
  unattributed_observations?: ExecutionEvent[]
  /** Client-side replay window used by the room to show hook/tool milestones. */
  recent_events?: ExecutionEvent[]
}

export type ExecutionProjectionDelta = {
  tasks?: ExecutionTask[]
  assignments?: ExecutionAssignment[]
  runs?: ExecutionRun[]
  agents?: ExecutionAgentInstance[]
  terminal_bindings?: ExecutionTerminalBinding[]
  relations?: ExecutionRelation[]
  removed?: {
    task_ids?: string[]
    assignment_ids?: string[]
    run_ids?: string[]
    agent_instance_ids?: string[]
    terminal_ids?: string[]
    relation_ids?: string[]
  }
}

export type ExecutionStreamMessage =
  | {
      type: 'event'
      cursor: number
      event: ExecutionEvent
      projection?: ExecutionProjectionDelta | null
    }
  | {
      type: 'ready'
      as_of_sequence: number
    }
  | {
      type: 'resync_required'
      after_sequence?: number
      latest_sequence?: number
      reason?: string
    }

export const ACTIVITY_LABELS: Record<RunActivity, string> = {
  idle: '空闲',
  thinking: '思考',
  planning: '规划',
  coding: '编码',
  tooling: '调用工具',
  testing: '测试',
  reviewing: '审查',
  waiting: '等待',
  finalizing: '整理结果',
  unknown: '阶段未知',
}

export const WAIT_REASON_LABELS: Record<WaitReason, string> = {
  user_input: '等待用户输入',
  approval: '等待批准',
  authentication: '等待认证',
  tool: '等待工具',
  child_run: '等待子任务',
  network: '等待网络',
  rate_limit: '等待限流恢复',
  retry_backoff: '等待重试',
  dependency: '等待依赖',
  resource: '等待资源',
  unknown: '等待原因未知',
}

export const EVIDENCE_SOURCE_LABELS: Record<EvidenceMode, string> = {
  reported: 'Agent 主动上报',
  adapter: 'Adapter 上报',
  observed: '系统观测',
  inferred: '系统推断',
  control: '控制面',
  stale: '已过期',
}

const TERMINAL_LIFECYCLES = new Set<RunLifecycle>([
  'succeeded',
  'failed',
  'cancelled',
  'lost',
])

export function timestampMilliseconds(value: ExecutionTimestamp | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null
    return Math.abs(value) < 1_000_000_000_000 ? value * 1000 : value
  }
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function evidenceExpiryMilliseconds(
  evidence: ExecutionFieldEvidence | null | undefined,
): number | null {
  if (!evidence) return null
  const explicit = timestampMilliseconds(evidence.expires_at)
  if (explicit !== null) return explicit
  if (evidence.valid_for_ms === null || evidence.valid_for_ms === undefined) return null
  const observedAt = timestampMilliseconds(evidence.observed_at ?? evidence.recorded_at)
  return observedAt === null ? null : observedAt + Math.max(0, evidence.valid_for_ms)
}

export function evidenceFreshness(
  evidence: ExecutionFieldEvidence | null | undefined,
  now = Date.now(),
): EvidenceFreshness {
  if (!evidence) return 'unknown'
  if (evidence.stale || evidence.source === 'stale') return 'stale'
  const expiresAt = evidenceExpiryMilliseconds(evidence)
  return expiresAt !== null && expiresAt <= now ? 'stale' : 'fresh'
}

/** Return the next field-evidence expiry strictly after now. The stream hook
 * uses this to schedule a UI generation even when no websocket frame arrives. */
export function nextEvidenceExpiry(
  snapshot: ExecutionSnapshot | null,
  now = Date.now(),
): number | null {
  if (!snapshot) return null
  let next: number | null = null
  for (const entity of [...snapshot.runs, ...snapshot.agents]) {
    for (const evidence of Object.values(entity.evidence ?? {})) {
      if (!evidence || evidence.stale || evidence.source === 'stale') continue
      const expiresAt = evidenceExpiryMilliseconds(evidence)
      if (expiresAt !== null && expiresAt > now && (next === null || expiresAt < next)) {
        next = expiresAt
      }
    }
  }
  return next
}

export function fieldEvidence(
  entity: { evidence?: ExecutionEvidence } | null | undefined,
  field: string,
): ExecutionFieldEvidence | null {
  return entity?.evidence?.[field] ?? null
}

export function isTerminalRun(run: ExecutionRun): boolean {
  return TERMINAL_LIFECYCLES.has(run.lifecycle)
}

export function runStatusEvidence(run: ExecutionRun): ExecutionFieldEvidence | null {
  if (isTerminalRun(run) || run.lifecycle === 'pending' || run.lifecycle === 'starting') {
    return fieldEvidence(run, 'lifecycle')
  }
  return fieldEvidence(run, 'activity') ?? fieldEvidence(run, 'lifecycle')
}

export function runStatusLabel(run: ExecutionRun, now = Date.now()): string {
  if (run.lifecycle === 'succeeded') return '完成'
  if (run.lifecycle === 'failed') return '报错'
  if (run.lifecycle === 'cancelled') return '已取消'
  if (run.lifecycle === 'lost') return '已失联'
  if (run.lifecycle === 'pending') return '待开始'
  if (run.lifecycle === 'starting') return '启动中'
  if (run.stale) return '状态过期'
  if (evidenceFreshness(fieldEvidence(run, 'activity'), now) === 'stale') return '状态过期'
  if (run.activity === 'waiting' && run.wait_reason) {
    return WAIT_REASON_LABELS[run.wait_reason] ?? WAIT_REASON_LABELS.unknown
  }
  return ACTIVITY_LABELS[run.activity ?? 'unknown']
}

export function normalizedProgress(progress: number | null | undefined): number | null {
  if (progress === null || progress === undefined || !Number.isFinite(progress)) return null
  return Math.min(1, Math.max(0, progress))
}

export function terminalBinding(
  snapshot: ExecutionSnapshot | null,
  terminalId: string,
): ExecutionTerminalBinding | null {
  return snapshot?.terminal_bindings.find((binding) => binding.terminal_id === terminalId) ?? null
}

export function activeRunForTerminal(
  snapshot: ExecutionSnapshot | null,
  terminalId: string,
): ExecutionRun | null {
  const runId = terminalBinding(snapshot, terminalId)?.active_run_id
  if (!runId) return null
  return snapshot?.runs.find((run) => run.run_id === runId) ?? null
}

export function activeAgentForTerminal(
  snapshot: ExecutionSnapshot | null,
  terminalId: string,
): ExecutionAgentInstance | null {
  const agentId = terminalBinding(snapshot, terminalId)?.active_agent_instance_id
  if (!agentId) return null
  return snapshot?.agents.find((agent) => agent.agent_instance_id === agentId) ?? null
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function records<T>(value: unknown): T[] {
  return Array.isArray(value) ? value.filter((item) => record(item) !== null) as T[] : []
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function textValue(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string') return value
    if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  }
  return ''
}

function optionalText(...values: unknown[]): string | null {
  const value = textValue(...values)
  return value || null
}

function numericValue(value: unknown, fallback = 0): number {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function optionalNumericValue(...values: unknown[]): number | null {
  for (const value of values) {
    if (value === null || value === undefined || value === '') continue
    const number = Number(value)
    if (Number.isFinite(number)) return number
  }
  return null
}

function normalizeEvidence(value: unknown): ExecutionEvidence | undefined {
  const source = record(value)
  if (!source) return undefined
  const result: ExecutionEvidence = {}
  for (const [field, rawValue] of Object.entries(source)) {
    const raw = record(rawValue)
    if (!raw) continue
    const mode = textValue(raw.source) as EvidenceMode
    result[field] = {
      source: mode || 'inferred',
      confidence: raw.confidence === null ? null : numericValue(raw.confidence, 1),
      observed_at: (raw.observed_at ?? raw.recorded_at) as ExecutionTimestamp | null | undefined,
      recorded_at: raw.recorded_at as ExecutionTimestamp | null | undefined,
      expires_at: raw.expires_at as ExecutionTimestamp | null | undefined,
      valid_for_ms: raw.valid_for_ms === null || raw.valid_for_ms === undefined
        ? null
        : numericValue(raw.valid_for_ms),
      fresh: typeof raw.fresh === 'boolean' ? raw.fresh : undefined,
      stale: raw.stale === true || raw.fresh === false,
      producer_id: optionalText(raw.producer_id),
      global_sequence: raw.global_sequence === undefined
        ? undefined
        : numericValue(raw.global_sequence),
    }
  }
  return result
}

function evidenceViewSequence(evidence: ExecutionEvidence | undefined): number {
  let sequence = 0
  for (const field of Object.values(evidence ?? {})) {
    if (field?.global_sequence !== undefined) {
      sequence = Math.max(sequence, numericValue(field.global_sequence))
    }
  }
  return sequence
}

function normalizeViewSequence(
  source: Record<string, unknown>,
  evidence: ExecutionEvidence | undefined,
  fallback: number,
): number {
  const evidenceSequence = evidenceViewSequence(evidence)
  const explicit = optionalNumericValue(source.view_sequence, source.evidence_sequence)
  if (explicit !== null) return Math.max(0, explicit, evidenceSequence)
  return Math.max(
    0,
    fallback,
    evidenceSequence,
    numericValue(source.last_global_sequence ?? source.last_sequence),
  )
}

/** The API deliberately exposes a generic projection envelope
 * (`id/state/attributes`) so every entity shares one storage contract. The UI
 * consumes typed, flat views; these adapters are the single boundary between
 * those representations and also accept already-flat fixtures/older servers. */
function normalizeTask(value: unknown, ownerId: string, fallbackViewSequence = 0): ExecutionTask | null {
  const source = record(value)
  if (!source) return null
  const state = record(source.state) ?? source
  const attributes = record(source.attributes) ?? source
  const taskId = textValue(source.task_id, source.id)
  if (!taskId) return null
  const updatedAt = (source.updated_at ?? state.updated_at ?? 0) as ExecutionTimestamp
  return {
    task_id: taskId,
    owner_id: textValue(source.owner_id, ownerId),
    context_id: optionalText(source.context_id, attributes.context_id),
    title: textValue(source.title, attributes.title, state.title) || `Task ${taskId.slice(0, 8)}`,
    status: textValue(source.status, source.lifecycle, state.lifecycle) || 'unknown',
    revision: numericValue(source.revision),
    view_sequence: normalizeViewSequence(source, undefined, fallbackViewSequence),
    created_at: (source.created_at ?? attributes.created_at ?? updatedAt) as ExecutionTimestamp,
    updated_at: updatedAt,
  }
}

function normalizeAssignment(
  value: unknown,
  ownerId: string,
  fallbackViewSequence = 0,
): ExecutionAssignment | null {
  const source = record(value)
  if (!source) return null
  const state = record(source.state) ?? source
  const attributes = record(source.attributes) ?? source
  const assignmentId = textValue(source.assignment_id, source.id)
  if (!assignmentId) return null
  return {
    assignment_id: assignmentId,
    owner_id: textValue(source.owner_id, ownerId),
    task_id: textValue(source.task_id, attributes.task_id),
    terminal_id: optionalText(source.terminal_id, attributes.terminal_id),
    device_id: optionalText(source.device_id, attributes.device_id),
    agent_instance_id: optionalText(source.agent_instance_id, attributes.agent_instance_id),
    status: textValue(source.status, source.lifecycle, state.lifecycle) || 'unknown',
    lease_expires_at: (source.lease_expires_at ?? state.lease_expires_at ?? null) as ExecutionTimestamp | null,
    revision: numericValue(source.revision),
    view_sequence: normalizeViewSequence(source, undefined, fallbackViewSequence),
    updated_at: (source.updated_at ?? state.updated_at) as ExecutionTimestamp | undefined,
  }
}

function normalizeRun(value: unknown, ownerId: string, fallbackViewSequence = 0): ExecutionRun | null {
  const source = record(value)
  if (!source) return null
  const state = record(source.state) ?? source
  const attributes = record(source.attributes) ?? source
  const runId = textValue(source.run_id, source.id)
  if (!runId) return null
  const updatedAt = (source.updated_at ?? state.updated_at ?? 0) as ExecutionTimestamp
  const progressValue = state.progress ?? source.progress
  const evidence = normalizeEvidence(source.evidence)
  return {
    run_id: runId,
    owner_id: textValue(source.owner_id, ownerId),
    task_id: textValue(source.task_id, attributes.task_id),
    assignment_id: textValue(source.assignment_id, attributes.assignment_id),
    parent_run_id: optionalText(source.parent_run_id, attributes.parent_run_id),
    terminal_id: optionalText(source.terminal_id, attributes.terminal_id),
    launch_id: optionalText(source.launch_id, attributes.launch_id),
    device_id: optionalText(source.device_id, attributes.device_id),
    agent_instance_id: optionalText(source.agent_instance_id, attributes.agent_instance_id),
    agent_kind: optionalText(source.agent_kind, attributes.agent_kind),
    lifecycle: (textValue(source.lifecycle, state.lifecycle) || 'pending') as RunLifecycle,
    activity: optionalText(source.activity, state.activity) as RunActivity | null,
    wait_reason: optionalText(source.wait_reason, state.wait_reason) as WaitReason | null,
    wait_target_run_id: optionalText(source.wait_target_run_id, state.wait_target_run_id),
    summary: optionalText(source.summary, state.summary),
    progress: progressValue === null || progressValue === undefined
      ? null
      : numericValue(progressValue),
    stale: state.stale === true || source.stale === true,
    attempt: numericValue(source.attempt ?? attributes.attempt, 1),
    revision: numericValue(source.revision),
    view_sequence: normalizeViewSequence(source, evidence, fallbackViewSequence),
    last_sequence: numericValue(source.last_sequence ?? source.last_global_sequence),
    created_at: (source.created_at ?? attributes.created_at ?? updatedAt) as ExecutionTimestamp,
    updated_at: updatedAt,
    evidence,
  }
}

function normalizeAgent(value: unknown, ownerId: string, fallbackViewSequence = 0): ExecutionAgentInstance | null {
  const source = record(value)
  if (!source) return null
  const state = record(source.state) ?? source
  const attributes = record(source.attributes) ?? source
  const agentId = textValue(source.agent_instance_id, source.id)
  if (!agentId) return null
  const capabilities = source.capabilities ?? attributes.capabilities
  const evidence = normalizeEvidence(source.evidence)
  return {
    agent_instance_id: agentId,
    owner_id: textValue(source.owner_id, ownerId),
    device_id: optionalText(source.device_id, attributes.device_id),
    terminal_id: optionalText(source.terminal_id, attributes.terminal_id),
    launch_id: optionalText(source.launch_id, attributes.launch_id),
    kind: optionalText(source.kind === 'agent_instance' ? null : source.kind, attributes.kind),
    lifecycle: textValue(source.lifecycle, state.lifecycle) || 'discovered',
    stale: state.stale === true || source.stale === true,
    cwd: optionalText(source.cwd, state.cwd, attributes.cwd),
    capabilities: Array.isArray(capabilities)
      ? capabilities.filter((item): item is string => typeof item === 'string')
      : undefined,
    last_seen_at: (source.last_seen_at ?? state.last_seen_at ?? state.last_heartbeat_occurred_at ?? null) as ExecutionTimestamp | null,
    revision: numericValue(source.revision),
    view_sequence: normalizeViewSequence(source, evidence, fallbackViewSequence),
    updated_at: (source.updated_at ?? state.updated_at) as ExecutionTimestamp | undefined,
    evidence,
  }
}

function normalizeTerminalBinding(
  value: unknown,
  fallbackViewSequence: number,
): ExecutionTerminalBinding | null {
  const source = record(value)
  if (!source) return null
  const terminalId = textValue(source.terminal_id, source.id)
  if (!terminalId) return null
  return {
    terminal_id: terminalId,
    active_run_id: optionalText(source.active_run_id),
    active_agent_instance_id: optionalText(source.active_agent_instance_id),
    revision: numericValue(source.revision),
    // Bindings are derived views and have no aggregate revision. The stream
    // cursor is their compatibility generation, so completion can clear an old
    // active_run_id even when last_global_sequence falls back to zero.
    view_sequence: normalizeViewSequence(source, undefined, fallbackViewSequence),
    updated_at: source.updated_at as ExecutionTimestamp | undefined,
  }
}

function normalizeRelation(
  value: unknown,
  ownerId: string,
  fallbackViewSequence: number,
): ExecutionRelation | null {
  const source = record(value)
  if (!source) return null
  const sourceEndpoint = record(source.source)
  const targetEndpoint = record(source.target)
  const relationId = textValue(source.relation_id, source.id)
  const relation = textValue(source.relation, source.relation_type, source.type)
  const sourceKind = textValue(source.source_kind, sourceEndpoint?.kind)
  const sourceId = textValue(source.source_id, sourceEndpoint?.id)
  const targetKind = textValue(source.target_kind, targetEndpoint?.kind)
  const targetId = textValue(source.target_id, targetEndpoint?.id)
  if (!relationId || !relation || !sourceKind || !sourceId || !targetKind || !targetId) return null
  return {
    relation_id: relationId,
    owner_id: textValue(source.owner_id, ownerId) || undefined,
    relation,
    source_kind: sourceKind,
    source_id: sourceId,
    target_kind: targetKind,
    target_id: targetId,
    attributes: record(source.attributes) ?? undefined,
    created_at: source.created_at as ExecutionTimestamp | undefined,
    revision: numericValue(source.revision),
    view_sequence: normalizeViewSequence(source, undefined, fallbackViewSequence),
  }
}

function normalizedItems<T>(values: unknown, convert: (value: unknown) => T | null): T[] {
  return records<unknown>(values).map(convert).filter((item): item is T => item !== null)
}

export function normalizeExecutionProjectionDelta(
  value: unknown,
  ownerId = '',
  cursor = 0,
): ExecutionProjectionDelta | null {
  const source = record(value)
  if (!source) return null
  const removed = record(source.removed)
  return {
    tasks: normalizedItems(source.tasks, (item) => normalizeTask(item, ownerId, cursor)),
    assignments: normalizedItems(
      source.assignments,
      (item) => normalizeAssignment(item, ownerId, cursor),
    ),
    runs: normalizedItems(source.runs, (item) => normalizeRun(item, ownerId, cursor)),
    agents: normalizedItems(source.agents, (item) => normalizeAgent(item, ownerId, cursor)),
    terminal_bindings: normalizedItems(
      source.terminal_bindings,
      (item) => normalizeTerminalBinding(item, cursor),
    ),
    relations: Array.isArray(source.relations)
      ? normalizedItems(source.relations, (item) => normalizeRelation(item, ownerId, cursor))
      : undefined,
    removed: removed ? {
      task_ids: strings(removed.task_ids),
      assignment_ids: strings(removed.assignment_ids),
      run_ids: strings(removed.run_ids),
      agent_instance_ids: strings(removed.agent_instance_ids),
      terminal_ids: strings(removed.terminal_ids),
      relation_ids: strings(removed.relation_ids),
    } : undefined,
  }
}

/** Keep the UI usable while a rolling deployment serves an older or partial
 * snapshot. Invalid collection fields become empty, never runtime exceptions. */
export function normalizeExecutionSnapshot(value: unknown): ExecutionSnapshot {
  const source = record(value) ?? {}
  const sequence = Number(source.as_of_sequence)
  const asOfSequence = Number.isFinite(sequence) && sequence >= 0 ? sequence : 0
  const ownerId = textValue(source.owner_id)
  return {
    schema: typeof source.schema === 'string' ? source.schema : 'agentserver.execution-snapshot/1',
    owner_id: ownerId || undefined,
    as_of_sequence: asOfSequence,
    tasks: normalizedItems(source.tasks, (item) => normalizeTask(item, ownerId, asOfSequence)),
    assignments: normalizedItems(
      source.assignments,
      (item) => normalizeAssignment(item, ownerId, asOfSequence),
    ),
    runs: normalizedItems(source.runs, (item) => normalizeRun(item, ownerId, asOfSequence)),
    agents: normalizedItems(source.agents, (item) => normalizeAgent(item, ownerId, asOfSequence)),
    terminal_bindings: normalizedItems(
      source.terminal_bindings,
      (item) => normalizeTerminalBinding(item, asOfSequence),
    ),
    relations: Array.isArray(source.relations)
      ? normalizedItems(source.relations, (item) => normalizeRelation(item, ownerId, asOfSequence))
      : undefined,
    unattributed_observations: records<ExecutionEvent>(source.unattributed_observations),
    recent_events: records<ExecutionEvent>(source.recent_events),
  }
}

function mergeByVersion<T extends { revision: number; view_sequence: number }>(
  current: T[],
  incoming: T[] | undefined,
  removed: string[] | undefined,
  id: (value: T) => string,
): T[] {
  if ((!incoming || incoming.length === 0) && (!removed || removed.length === 0)) return current
  const values = new Map(current.map((value) => [id(value), value]))
  for (const value of incoming ?? []) {
    const key = id(value)
    const existing = values.get(key)
    if (
      !existing
      || value.revision > existing.revision
      || (
        value.revision === existing.revision
        && numericValue(value.view_sequence) > numericValue(existing.view_sequence)
      )
    ) {
      values.set(key, value)
    }
  }
  for (const key of removed ?? []) values.delete(key)
  return [...values.values()]
}

export function applyExecutionMessage(
  snapshot: ExecutionSnapshot,
  message: ExecutionStreamMessage,
): ExecutionSnapshot | null {
  if (message.type === 'resync_required') return null
  if (message.type === 'ready') return snapshot
  const sequence = Number(message.cursor)
  if (!Number.isFinite(sequence) || sequence <= snapshot.as_of_sequence) return snapshot
  const recentEvents = [
    ...(snapshot.recent_events ?? []),
    message.event,
  ].slice(-64)
  const projection = normalizeExecutionProjectionDelta(
    message.projection ?? message.event.normalized_state,
    snapshot.owner_id,
    sequence,
  )
  if (!projection) return { ...snapshot, as_of_sequence: sequence, recent_events: recentEvents }
  const removed = projection.removed
  let relations = projection.relations === undefined
    ? snapshot.relations
    : projection.relations
  if (relations !== undefined && removed?.relation_ids) {
    relations = relations.filter(
      (relation) => !removed.relation_ids?.includes(relation.relation_id),
    )
  }
  return {
    ...snapshot,
    as_of_sequence: sequence,
    recent_events: recentEvents,
    tasks: mergeByVersion(snapshot.tasks, projection.tasks, removed?.task_ids, (task) => task.task_id),
    assignments: mergeByVersion(
      snapshot.assignments,
      projection.assignments,
      removed?.assignment_ids,
      (assignment) => assignment.assignment_id,
    ),
    runs: mergeByVersion(snapshot.runs, projection.runs, removed?.run_ids, (run) => run.run_id),
    agents: mergeByVersion(
      snapshot.agents,
      projection.agents,
      removed?.agent_instance_ids,
      (agent) => agent.agent_instance_id,
    ),
    terminal_bindings: mergeByVersion(
      snapshot.terminal_bindings,
      projection.terminal_bindings,
      removed?.terminal_ids,
      (binding) => binding.terminal_id,
    ),
    relations,
  }
}

function compareRuns(left: ExecutionRun, right: ExecutionRun): number {
  const sequenceDelta = (left.last_sequence ?? 0) - (right.last_sequence ?? 0)
  if (sequenceDelta) return sequenceDelta
  return (timestampMilliseconds(left.created_at ?? left.updated_at) ?? 0)
    - (timestampMilliseconds(right.created_at ?? right.updated_at) ?? 0)
}

function parentRunRelations(snapshot: ExecutionSnapshot): ExecutionRelation[] | null {
  if (snapshot.relations === undefined) return null
  return snapshot.relations.filter(
    (relation) => relation.relation === 'parent_run'
      && relation.source_kind === 'run'
      && relation.target_kind === 'run',
  )
}

export function parentRunIds(snapshot: ExecutionSnapshot, childRunId: string): string[] {
  const relations = parentRunRelations(snapshot)
  if (relations === null) {
    const run = snapshot.runs.find((item) => item.run_id === childRunId)
    return run?.parent_run_id ? [run.parent_run_id] : []
  }
  return [...new Set(
    relations
      .filter((relation) => relation.target_id === childRunId)
      .map((relation) => relation.source_id),
  )]
}

export function runsByParent(
  snapshot: ExecutionSnapshot,
  parentRunId: string | null,
): ExecutionRun[] {
  const relations = parentRunRelations(snapshot)
  const children = relations === null
    ? snapshot.runs.filter((run) => (run.parent_run_id ?? null) === parentRunId)
    : parentRunId === null
      ? snapshot.runs.filter((run) => parentRunIds(snapshot, run.run_id).length === 0)
      : snapshot.runs.filter((run) => relations.some(
          (relation) => relation.source_id === parentRunId && relation.target_id === run.run_id,
        ))
  return [...new Map(children.map((run) => [run.run_id, run])).values()].sort(compareRuns)
}

/** Return every reachable DAG root for the selected Run. Multiple roots are
 * possible for relation-backed snapshots; a malformed cycle falls back to the
 * selected Run and is stopped by RunTree's per-path cycle guard. */
export function runTreeRoots(
  snapshot: ExecutionSnapshot,
  selected: ExecutionRun,
): ExecutionRun[] {
  const byId = new Map(snapshot.runs.map((run) => [run.run_id, run]))
  const roots = new Map<string, ExecutionRun>()
  const visit = (run: ExecutionRun, path: ReadonlySet<string>) => {
    const parents = parentRunIds(snapshot, run.run_id)
      .map((id) => byId.get(id))
      .filter((item): item is ExecutionRun => item !== undefined)
    if (parents.length === 0) {
      roots.set(run.run_id, run)
      return
    }
    const nextPath = new Set(path)
    nextPath.add(run.run_id)
    for (const parent of parents) {
      if (!nextPath.has(parent.run_id)) visit(parent, nextPath)
    }
  }
  visit(selected, new Set())
  return (roots.size ? [...roots.values()] : [selected]).sort(compareRuns)
}

/** Shared by the stream hook and directly unit tested: concurrent resync
 * requests receive the same Promise; a later request starts a new operation. */
export function createSingleFlight<T>(): (operation: () => Promise<T>) => Promise<T> {
  let current: Promise<T> | null = null
  return (operation) => {
    if (current) return current
    const next = Promise.resolve().then(operation)
    current = next
    void next.finally(() => {
      if (current === next) current = null
    }).catch(() => undefined)
    return next
  }
}
