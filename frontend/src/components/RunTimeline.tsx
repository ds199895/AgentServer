import { useCallback, useEffect, useRef, useState } from 'react'
import { Clock3, RefreshCw } from 'lucide-react'

import { RunStatusBadge } from '@/components/RunStatusBadge'
import { RunTree } from '@/components/RunTree'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useExecutionContext } from '@/execution-context'
import { fetchRunEvents } from '@/execution-api'
import { mergeExecutionEvents } from '@/execution-events'
import {
  EVIDENCE_SOURCE_LABELS,
  evidenceFreshness,
  fieldEvidence,
  type ExecutionEvent,
  type ExecutionFieldEvidence,
  type ExecutionRun,
  type ExecutionSnapshot,
} from '@/execution-state'

const EVENT_LABELS: Record<string, string> = {
  'run.requested': '请求运行',
  'run.started': '开始运行',
  'run.activity.changed': '阶段变化',
  'run.progress.updated': '进度更新',
  'run.input.requested': '请求输入',
  'run.input.provided': '已提供输入',
  'run.cancel.requested': '请求取消',
  'run.succeeded': '运行完成',
  'run.failed': '运行报错',
  'run.cancelled': '运行取消',
  'run.stale': '状态过期',
  'run.lost': '运行失联',
  'span.started': '步骤开始',
  'span.updated': '步骤更新',
  'span.ended': '步骤结束',
  'artifact.published': '产出 Artifact',
  'state.conflict.detected': '状态冲突',
}

const TIMELINE_PAGE_SIZE = 300

function timestamp(value: number | string): string {
  const numeric = typeof value === 'number' && Math.abs(value) < 1_000_000_000_000
    ? value * 1000
    : value
  const date = new Date(numeric)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

function evidenceDescription(evidence: ExecutionFieldEvidence | null, now: number): string {
  if (!evidence) return '来源未知 · 新鲜度未知 · 置信度未知'
  const confidence = evidence.confidence === null || evidence.confidence === undefined
    ? '置信度未知'
    : `置信度 ${Math.round(Math.min(1, Math.max(0, evidence.confidence)) * 100)}%`
  const freshness = evidenceFreshness(evidence, now)
  const source = EVIDENCE_SOURCE_LABELS[evidence.source] ?? evidence.source
  return `${source} · ${freshness === 'fresh' ? '新鲜' : freshness === 'stale' ? '已过期' : '新鲜度未知'} · ${confidence}`
}

function EvidenceRow({
  label,
  value,
  evidence,
  now,
}: {
  label: string
  value: string
  evidence: ExecutionFieldEvidence | null
  now: number
}) {
  return (
    <div className="grid min-w-0 gap-1 rounded-md border border-[#25313a] bg-[#0b1117] p-2">
      <small className="text-[8px] text-[#65747f]">{label}</small>
      <strong className="truncate text-[10px] text-[#dce5eb]">{value}</strong>
      <small className="truncate font-mono text-[7px] text-[#72828e]" title={evidenceDescription(evidence, now)}>
        {evidenceDescription(evidence, now)}
      </small>
    </div>
  )
}

export function RunTimeline({
  snapshot,
  run,
  open,
  onOpenChange,
}: {
  snapshot: ExecutionSnapshot
  run: ExecutionRun
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const execution = useExecutionContext()
  const [events, setEvents] = useState<ExecutionEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [generation, setGeneration] = useState(0)
  const [nextSequence, setNextSequence] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [initialized, setInitialized] = useState(false)
  const [checkedThrough, setCheckedThrough] = useState(0)
  const requestIdRef = useRef(0)
  const requestControllerRef = useRef<AbortController | null>(null)

  const loadPage = useCallback(async ({
    afterSequence,
    replace,
    live = false,
    checkedTarget,
  }: {
    afterSequence: number
    replace: boolean
    live?: boolean
    checkedTarget?: number
  }) => {
    const requestId = ++requestIdRef.current
    requestControllerRef.current?.abort()
    const controller = new AbortController()
    requestControllerRef.current = controller
    setLoading(true)
    setError('')
    try {
      const page = await fetchRunEvents(run.run_id, {
        afterSequence,
        limit: TIMELINE_PAGE_SIZE,
        signal: controller.signal,
      })
      if (controller.signal.aborted || requestId !== requestIdRef.current) return
      if (page.resync_required && !replace) {
        setGeneration((current) => current + 1)
        return
      }
      setEvents((current) => replace
        ? mergeExecutionEvents([], page.events)
        : mergeExecutionEvents(current, page.events))
      // Live catch-up starts at the previous REST high-water mark and must not
      // disturb the independent cursor used to page older history.
      if (!live) {
        setNextSequence(page.next_sequence)
        setHasMore(page.has_more)
      }
      const through = live && page.has_more
        ? page.next_sequence
        : page.as_of_sequence ?? checkedTarget
      if (through !== undefined && through !== null) {
        setCheckedThrough((current) => Math.max(current, through))
      }
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === 'AbortError')) {
        setError(reason instanceof Error ? reason.message : '无法读取运行时间线')
        // A failed live catch-up waits for the next websocket generation or an
        // explicit retry instead of forming a render/fetch retry loop.
        if (checkedTarget !== undefined) {
          setCheckedThrough((current) => Math.max(current, checkedTarget))
        }
      }
    } finally {
      if (!controller.signal.aborted && requestId === requestIdRef.current) {
        setLoading(false)
        if (replace) setInitialized(true)
      }
    }
  }, [run.run_id])

  useEffect(() => {
    if (!open) return
    setEvents([])
    setNextSequence(0)
    setHasMore(false)
    setInitialized(false)
    setCheckedThrough(0)
    void loadPage({ afterSequence: 0, replace: true })
    return () => {
      requestIdRef.current += 1
      requestControllerRef.current?.abort()
      requestControllerRef.current = null
    }
  }, [generation, loadPage, open])

  // Snapshot cursor is the websocket high-water mark. Fetching strictly after
  // the last checked REST high-water appends matching run/span/artifact events
  // while preserving a separate cursor for still-unloaded historical pages.
  useEffect(() => {
    if (
      !open
      || !initialized
      || loading
      || snapshot.as_of_sequence <= checkedThrough
    ) return
    void loadPage({
      afterSequence: checkedThrough,
      replace: false,
      live: true,
      checkedTarget: snapshot.as_of_sequence,
    })
  }, [checkedThrough, initialized, loadPage, loading, open, snapshot.as_of_sequence])

  const progress = run.progress === null || run.progress === undefined
    ? '未知'
    : `${Math.round(Math.min(1, Math.max(0, run.progress)) * 100)}%`
  const agent = run.agent_instance_id
    ? snapshot.agents.find((item) => item.agent_instance_id === run.agent_instance_id) ?? null
    : null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="grid max-h-[min(840px,calc(100dvh-2rem))] w-[min(920px,calc(100vw-2rem))] max-w-none grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden border-[#34434e] bg-[#0b1015] p-0 sm:max-w-none">
        <DialogHeader className="border-b border-[#25313a] bg-[#0e151b] px-5 py-4 pr-12">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <DialogTitle className="mr-auto truncate text-base text-[#edf4f9]">
              {run.summary || `Run ${run.run_id.slice(0, 8)}`}
            </DialogTitle>
            <RunStatusBadge run={run} agent={agent} />
          </div>
          <DialogDescription className="font-mono text-[9px] text-[#71808b]">
            {run.run_id} · revision {run.revision}
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 grid-cols-[minmax(0,1fr)_310px] max-lg:grid-cols-1 max-lg:overflow-auto">
          <section className="min-h-0 overflow-auto border-r border-[#25313a] p-4 max-lg:overflow-visible max-lg:border-r-0 max-lg:border-b">
            <header className="mb-3 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-[10px] font-semibold text-[#aab8c2]">
                <Clock3 className="size-3.5 text-[#7bbd9e]" />
                运行时间线
              </div>
              <Button
                variant="outline"
                size="xs"
                disabled={loading}
                onClick={() => setGeneration((current) => current + 1)}
              >
                <RefreshCw className={loading ? 'animate-spin motion-reduce:animate-none' : ''} />
                刷新
              </Button>
            </header>
            {error && <p role="alert" className="rounded-md border border-[#713640] bg-[#28171b] p-2 text-[9px] text-[#ff9aa4]">{error}</p>}
            {!error && !loading && events.length === 0 && (
              <p className="rounded-md border border-dashed border-[#2b3741] p-5 text-center text-[9px] text-[#687985]">暂无可显示的运行事件</p>
            )}
            <ol className="grid gap-2">
              {events.map((event) => (
                <li key={event.event_id} className="grid grid-cols-[8px_minmax(0,1fr)] gap-2.5">
                  <div className="relative flex justify-center before:absolute before:top-2 before:bottom-[-10px] before:w-px before:bg-[#293641] last:before:hidden">
                    <i className="relative z-[1] mt-1.5 size-2 rounded-full border border-[#47705e] bg-[#122019]" />
                  </div>
                  <article className="rounded-md border border-[#25313a] bg-[#0d141a] px-3 py-2">
                    <div className="flex min-w-0 items-center justify-between gap-2">
                      <strong className="truncate text-[10px] text-[#dce5eb]">{EVENT_LABELS[event.type] ?? event.type}</strong>
                      <time className="flex-none font-mono text-[7px] text-[#62727e]">{timestamp(event.recorded_at)}</time>
                    </div>
                    {typeof event.payload.summary === 'string' && event.payload.summary && (
                      <p className="mt-1.5 mb-0 text-[9px] leading-4 text-[#93a1ab]">{event.payload.summary}</p>
                    )}
                    <small className="mt-1.5 block truncate font-mono text-[7px] text-[#576670]">
                      seq {event.global_sequence} · {event.producer.mode}
                    </small>
                  </article>
                </li>
              ))}
            </ol>
            {hasMore && (
              <Button
                variant="outline"
                size="sm"
                className="mt-3 w-full"
                disabled={loading}
                onClick={() => void loadPage({ afterSequence: nextSequence, replace: false })}
              >
                {loading ? '加载中…' : '加载更多'}
              </Button>
            )}
          </section>
          <aside className="min-h-0 overflow-auto p-4 max-lg:overflow-visible">
            <div className="mb-4 grid grid-cols-2 gap-2">
              <EvidenceRow label="生命周期" value={run.lifecycle} evidence={fieldEvidence(run, 'lifecycle')} now={execution.freshness_now} />
              <EvidenceRow label="活动阶段" value={run.activity || 'unknown'} evidence={fieldEvidence(run, 'activity')} now={execution.freshness_now} />
              <EvidenceRow label="等待原因" value={run.wait_reason || '—'} evidence={fieldEvidence(run, 'wait_reason')} now={execution.freshness_now} />
              <EvidenceRow label="进度" value={progress} evidence={fieldEvidence(run, 'progress')} now={execution.freshness_now} />
            </div>
            <RunTree snapshot={snapshot} run={run} />
          </aside>
        </div>
      </DialogContent>
    </Dialog>
  )
}
