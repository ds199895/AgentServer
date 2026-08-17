import { AlertTriangle, RefreshCw } from 'lucide-react'

import { useExecutionContext } from '@/execution-context'

export function ExecutionStatusNotice() {
  const execution = useExecutionContext()
  if (!['degraded', 'reconnecting', 'unauthorized'].includes(execution.status)) return null

  const unauthorized = execution.status === 'unauthorized'
  const reconnecting = execution.status === 'reconnecting'
  const title = unauthorized
    ? 'Agent 状态同步未授权'
    : reconnecting
      ? 'Agent 状态连接已中断'
      : 'Agent 状态同步已降级'
  const detail = execution.error || (reconnecting
    ? '正在重连；终端仍可正常使用，当前状态可能不是最新。'
    : '终端仍可正常使用，当前状态可能不是最新。')

  return (
    <aside
      role="alert"
      className="fixed right-5 bottom-20 z-[80] flex max-w-[min(430px,calc(100vw-2rem))] items-center gap-2.5 rounded-lg border border-[#705d38] bg-[#2a2215ee] px-3 py-2 text-[#e5bc70] shadow-[0_14px_40px_#0008] backdrop-blur max-md:right-3 max-md:bottom-16 max-md:left-3 max-md:max-w-none"
    >
      <AlertTriangle className="size-4 flex-none" />
      <span className="min-w-0 flex-1">
        <strong className="block text-[10px]">{title}</strong>
        <small className="block truncate text-[8px] opacity-80" title={detail}>{detail}</small>
      </span>
      <button
        type="button"
        onClick={execution.refresh}
        className="inline-flex h-7 flex-none cursor-pointer items-center gap-1 rounded border border-current/35 px-2 text-[8px] font-semibold hover:bg-white/5"
      >
        <RefreshCw className="size-3" />
        重试
      </button>
    </aside>
  )
}
