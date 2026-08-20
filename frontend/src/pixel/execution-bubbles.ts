import type { ExecutionEvent, ExecutionSnapshot, RunActivity } from '../execution-state'
import type { BubbleTone } from './bubbles'

export type EventBubble = { key: string; text: string; tone: BubbleTone }

const ACTIVITY_TEXT: Partial<Record<RunActivity, string>> = {
  idle: '空闲',
  thinking: '思考中…',
  planning: '规划中…',
  coding: '写代码…',
  tooling: '调用工具…',
  testing: '跑测试…',
  reviewing: '审查中…',
  waiting: '等待中…',
  finalizing: '整理结果…',
}

function shortEventText(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const text = value.replace(/\s+/g, ' ').trim()
  if (!text) return null
  return text.length > 22 ? `${text.slice(0, 21)}…` : text
}

export function executionEventBubble(event: ExecutionEvent): EventBubble | null {
  const payload = event.payload || {}
  const summary = shortEventText(payload.summary)
  if (summary) return { key: `${event.global_sequence}:${event.type}`, text: summary, tone: 'mute' }
  const name = shortEventText(payload.name || payload.tool_name)
  let text: string | null = null
  let tone: BubbleTone = 'mute'
  switch (event.type) {
    case 'agent.registered': text = 'Agent 上线'; tone = 'ok'; break
    case 'agent.stopping': text = 'Agent 停止'; break
    case 'agent.unreachable':
    case 'agent.lost': text = 'Agent 失联'; tone = 'bad'; break
    case 'run.started': text = '开始执行'; tone = 'work'; break
    case 'run.succeeded': text = '已完成'; tone = 'ok'; break
    case 'run.failed': text = '出错了'; tone = 'bad'; break
    case 'run.cancelled': text = '已取消'; break
    case 'run.lost': text = '连接丢失'; tone = 'bad'; break
    case 'run.stale': text = '状态过期'; break
    case 'run.recovered': text = '状态已恢复'; tone = 'ok'; break
    case 'run.activity.changed': {
      const activity = String(payload.activity || 'unknown') as RunActivity
      text = ACTIVITY_TEXT[activity] || '阶段变化'
      tone = activity === 'thinking' || activity === 'planning' || activity === 'reviewing'
        ? 'think'
        : activity === 'testing' ? 'test' : activity === 'waiting' ? 'wait' : 'work'
      break
    }
    case 'run.progress.updated': {
      const progress = Number(payload.progress)
      text = Number.isFinite(progress) ? `进度 ${Math.round(progress * 100)}%` : '进度更新'
      tone = 'test'
      break
    }
    case 'run.input.requested': text = '等待输入'; tone = 'wait'; break
    case 'run.input.provided': text = '收到输入'; tone = 'work'; break
    case 'span.started': text = name ? `调用 ${name}` : '调用工具'; tone = 'work'; break
    case 'span.ended': {
      const failed = String(payload.outcome || '') === 'failed'
      text = name ? `${name}${failed ? ' 失败' : ' 完成'}` : (failed ? '工具失败' : '工具完成')
      tone = failed ? 'bad' : 'ok'
      break
    }
    case 'child_run.requested': text = '开始子任务'; tone = 'work'; break
    case 'child_run.linked': text = '子任务已连接'; tone = 'ok'; break
    default: return null
  }
  return text ? { key: `${event.global_sequence}:${event.type}`, text, tone } : null
}

export function eventBubbleForTerminal(
  snapshot: ExecutionSnapshot,
  terminalId: string,
): EventBubble | null {
  const runIds = new Set(snapshot.runs
    .filter((run) => run.terminal_id === terminalId)
    .map((run) => run.run_id))
  const agentIds = new Set(snapshot.agents
    .filter((agent) => agent.terminal_id === terminalId)
    .map((agent) => agent.agent_instance_id))
  const relevant: ExecutionEvent[] = []
  for (const event of [...(snapshot.recent_events ?? [])].reverse()) {
    if (event.scope.terminal_id !== terminalId
      && (!event.scope.run_id || !runIds.has(event.scope.run_id))
      && (!event.scope.agent_instance_id || !agentIds.has(event.scope.agent_instance_id))) continue
    if (executionEventBubble(event)) relevant.push(event)
    if (relevant.length === 2) break
  }
  const latest = relevant[0]
  if (!latest) return null
  const previous = relevant[1]
  // Provider PostTool hooks emit span.ended and then a generic transition back
  // to thinking. Preserve the specific tool result when the two facts are the
  // same producer's adjacent events.
  const adjacentToolResult = latest.type === 'run.activity.changed'
    && previous?.type === 'span.ended'
    && latest.producer?.id === previous.producer?.id
    && latest.producer?.epoch === previous.producer?.epoch
    && Number(latest.producer?.seq) === Number(previous.producer?.seq) + 1
  return executionEventBubble(adjacentToolResult ? previous : latest)
}
