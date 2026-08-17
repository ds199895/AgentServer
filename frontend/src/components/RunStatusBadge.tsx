import type { ReactNode } from 'react'

import { useExecutionContext } from '@/execution-context'
import {
  EVIDENCE_SOURCE_LABELS,
  evidenceFreshness,
  fieldEvidence,
  normalizedProgress,
  runStatusEvidence,
  runStatusLabel,
  type EvidenceFreshness,
  type ExecutionAgentInstance,
  type ExecutionFieldEvidence,
  type ExecutionRun,
} from '@/execution-state'
import { cn } from '@/lib/utils'

const AGENT_NAMES: Record<string, string> = {
  codex: 'Codex',
  claude: 'Claude',
  kimi: 'Kimi',
  deepseek: 'DeepSeek',
}

function agentName(run: ExecutionRun | null, agent: ExecutionAgentInstance | null): string {
  const kind = agent?.kind || run?.agent_kind
  return kind ? (AGENT_NAMES[kind] ?? kind) : 'Agent'
}

function agentEvidence(agent: ExecutionAgentInstance | null): ExecutionFieldEvidence | null {
  return fieldEvidence(agent, 'lifecycle') ?? fieldEvidence(agent, 'liveness')
}

function agentStatusLabel(agent: ExecutionAgentInstance | null): string {
  return ({
    discovered: '已发现',
    starting: '启动中',
    online: '在线',
    stopping: '停止中',
    unreachable: '暂时失联',
    exited: '已退出',
    lost: '已失联',
  } as Record<string, string>)[agent?.lifecycle ?? ''] ?? '阶段未知'
}

function confidenceLabel(evidence: ExecutionFieldEvidence | null): string {
  if (evidence?.confidence === null || evidence?.confidence === undefined) return '置信度未知'
  return `置信度 ${Math.round(Math.min(1, Math.max(0, evidence.confidence)) * 100)}%`
}

function freshnessLabel(freshness: EvidenceFreshness): string {
  if (freshness === 'fresh') return '状态新鲜'
  if (freshness === 'stale') return '状态已过期'
  return '新鲜度未知'
}

function appearance(
  run: ExecutionRun | null,
  agent: ExecutionAgentInstance | null,
  freshness: EvidenceFreshness,
): string {
  if (freshness === 'stale') return 'border-[#705d38] bg-[#2a2215] text-[#e5bc70]'
  if (
    run?.lifecycle === 'failed'
    || run?.lifecycle === 'lost'
    || agent?.lifecycle === 'lost'
    || agent?.lifecycle === 'unreachable'
  ) {
    return 'border-[#713640] bg-[#28171b] text-[#ff9aa4]'
  }
  if (run?.lifecycle === 'succeeded') return 'border-[#315a48] bg-[#15251e] text-primary'
  if (run?.activity === 'waiting') return 'border-[#66522d] bg-[#251f14] text-[#e9bd68]'
  if (run?.activity === 'thinking' || run?.activity === 'planning') {
    return 'border-[#51416d] bg-[#201a2a] text-[#c9a9f4]'
  }
  if (run?.activity === 'testing') return 'border-[#31556b] bg-[#14222b] text-[#91cce8]'
  return 'border-[#2b4b3d] bg-[#122019] text-[#9adfca]'
}

export function RunStatusBadge({
  run,
  agent = null,
  compact = false,
  onClick,
}: {
  run: ExecutionRun | null
  agent?: ExecutionAgentInstance | null
  compact?: boolean
  onClick?: () => void
}) {
  const execution = useExecutionContext()
  if (!run && !agent) return null
  const evidence = run ? runStatusEvidence(run) : agentEvidence(agent)
  const freshness = run?.stale || agent?.stale
    ? 'stale'
    : evidenceFreshness(evidence, execution.freshness_now)
  const source = evidence ? (EVIDENCE_SOURCE_LABELS[evidence.source] ?? evidence.source) : '来源未知'
  const confidence = confidenceLabel(evidence)
  const label = run ? runStatusLabel(run, execution.freshness_now) : agentStatusLabel(agent)
  const progress = normalizedProgress(run?.progress)
  const details = `${agentName(run, agent)} · ${label} · ${source} · ${freshnessLabel(freshness)} · ${confidence}`
  const content: ReactNode = (
    <>
      <i
        aria-hidden="true"
        className={cn(
          'size-1.5 flex-none rounded-full bg-current opacity-75',
          run?.lifecycle === 'running' && freshness === 'fresh' && 'animate-world-pulse motion-reduce:animate-none',
        )}
      />
      <strong className="truncate font-mono text-[8px] font-semibold">{agentName(run, agent)}</strong>
      <span aria-hidden="true" className="opacity-45">·</span>
      <span className="truncate font-mono text-[8px]">{label}</span>
      {!compact && (
        <>
          <span aria-hidden="true" className="opacity-45">·</span>
          <span className="truncate font-mono text-[8px] opacity-75">{source}</span>
          <span className="font-mono text-[8px] opacity-65">{freshness === 'stale' ? '过期' : confidence}</span>
        </>
      )}
      {progress !== null && (
        <span
          aria-label={`进度 ${Math.round(progress * 100)}%`}
          className="relative h-1 w-9 flex-none overflow-hidden rounded-full bg-black/35"
        >
          <i className="absolute inset-y-0 left-0 rounded-full bg-current" style={{ width: `${progress * 100}%` }} />
        </span>
      )}
    </>
  )
  const common = cn(
    'inline-flex min-w-0 items-center gap-1 rounded border px-1.5 py-0.5 text-left',
    appearance(run, agent, freshness),
    onClick && 'cursor-pointer transition-colors hover:border-primary focus-visible:outline-1 focus-visible:outline-primary',
  )
  const data = {
    'data-run-id': run?.run_id,
    'data-run-lifecycle': run?.lifecycle,
    'data-run-activity': run?.activity ?? 'unknown',
    'data-run-source': evidence?.source ?? 'unknown',
    'data-run-freshness': freshness,
    'data-run-confidence': evidence?.confidence ?? undefined,
  }
  return onClick ? (
    <button type="button" className={common} onClick={onClick} title={details} aria-label={`${details}，打开运行时间线`} {...data}>
      {content}
    </button>
  ) : (
    <span role="status" className={common} title={details} aria-label={details} {...data}>
      {content}
    </span>
  )
}
