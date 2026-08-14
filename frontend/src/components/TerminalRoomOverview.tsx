import { lazy, Suspense, useState } from 'react'
import { Maximize2, Minus, XIcon } from 'lucide-react'

import type { Device, TerminalSession } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

const DeviceWorld = lazy(() => import('@/DeviceWorld'))

const loadingFallback = (
  <div className="grid h-full w-full place-items-center bg-[#071018]">
    <span className="size-[7px] animate-world-pulse rounded-full bg-primary shadow-[0_0_10px_var(--color-primary)]" />
  </div>
)

const MOBILE_QUERY = '(max-width: 767.98px)'

function isMobileViewport(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches
}

type Props = {
  devices: Device[]
  sessions: TerminalSession[]
  busyId: string | null
  activeSessionId: string | null
  onOpen: (device: Device) => void
  onProbe: (device: Device) => void
  onEdit: (device: Device) => void
  onSelectTerminal: (sessionId: string) => void
}

export function TerminalRoomOverview({ devices, sessions, busyId, activeSessionId, onOpen, onProbe, onEdit, onSelectTerminal }: Props) {
  const [expanded, setExpanded] = useState(false)
  // 手机默认收起为小圆点，点击圆点直接打开弹窗，不经过悬浮窗。
  const [collapsed, setCollapsed] = useState(isMobileViewport)
  return (
    <>
      {collapsed ? (
        <button
          aria-label="打开设备房间总览"
          title="打开设备房间总览"
          onClick={() => {
            if (isMobileViewport()) setExpanded(true)
            else setCollapsed(false)
          }}
          className="absolute top-[60px] right-6 z-[7] grid size-7 cursor-pointer place-items-center rounded-full border border-[#365047] bg-[#06100ed9] shadow-[0_8px_25px_#000a] backdrop-blur-[7px] transition-transform hover:scale-110 max-md:top-[50px] max-md:right-3.5"
        >
          <i className="size-2 rounded-full bg-primary shadow-[0_0_8px_var(--color-primary)]" />
        </button>
      ) : (
      <div
        role="button"
        tabIndex={0}
        aria-label="放大设备房间总览"
        title="点击放大设备房间总览"
        onClick={() => setExpanded(true)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            setExpanded(true)
          }
        }}
        className="absolute top-[60px] right-6 z-[7] aspect-video w-[clamp(190px,20vw,280px)] cursor-zoom-in overflow-hidden rounded-[10px] border border-[#365047] bg-[#05090e] shadow-[0_16px_45px_#000b,0_0_0_1px_#77f2b40c] outline-none transition-[border-color,box-shadow,transform] hover:-translate-y-0.5 hover:border-[#62b58e] hover:shadow-[0_18px_50px_#000d,0_0_0_2px_#77f2b426] focus-visible:-translate-y-0.5 focus-visible:border-[#62b58e] focus-visible:shadow-[0_18px_50px_#000d,0_0_0_2px_#77f2b426] max-md:top-[50px] max-md:right-3.5 max-md:w-[min(190px,46vw)]"
      >
        <Suspense fallback={loadingFallback}>
          <DeviceWorld compact devices={devices} sessions={sessions} busyId={busyId} activeSessionId={activeSessionId} onOpen={onOpen} onProbe={onProbe} onEdit={onEdit} onSelectTerminal={onSelectTerminal} />
        </Suspense>
        <span className="pointer-events-none absolute top-[7px] left-[7px] z-[5] flex items-center gap-[5px] rounded-xl border border-[#365047] bg-[#06100ed9] px-[7px] py-[5px] font-mono text-[8px] text-[#d7e6df] shadow-[0_5px_15px_#0008] backdrop-blur-[7px]">
          <i className="size-1.5 rounded-full bg-primary shadow-[0_0_8px_var(--color-primary)]" />
          {devices.filter((device) => device.frp_online).length}/{devices.length}
        </span>
        <button
          aria-label="收起设备房间总览"
          title="收起为小圆点"
          onClick={(event) => {
            event.stopPropagation()
            setCollapsed(true)
          }}
          onKeyDown={(event) => event.stopPropagation()}
          className="absolute top-[7px] right-[38px] z-[6] grid size-[25px] cursor-pointer place-items-center rounded-[7px] border border-[#365047] bg-[#06100ed9] text-[#b9c5ce] shadow-[0_5px_15px_#0008] backdrop-blur-[7px] transition-colors hover:text-primary"
        >
          <Minus className="size-3" />
        </button>
        <span aria-hidden="true" className="pointer-events-none absolute top-[7px] right-[7px] z-[5] grid size-[25px] place-items-center rounded-[7px] border border-[#365047] bg-[#06100ed9] text-primary shadow-[0_5px_15px_#0008] backdrop-blur-[7px]">
          <Maximize2 className="size-3" />
        </span>
      </div>
      )}
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent
          showCloseButton={false}
          className="grid aspect-[4/3] h-auto w-[min(1180px,calc(100vw-2rem),calc(133.333dvh-2.667rem))] max-w-none grid-rows-[68px_minmax(0,1fr)] gap-0 overflow-hidden rounded-[15px] border-[#344b53] bg-[#080d12] p-0 shadow-[0_35px_100px_#000e,0_0_45px_#77f2b412] sm:max-w-none max-md:grid-rows-[58px_minmax(0,1fr)] [&_.device-world-empty]:h-full [&_.device-world-empty]:min-h-0 [&_.device-world-empty]:rounded-none [&_.device-world-empty]:border-0 [&_.device-world-shell]:min-h-0 [&_.device-world-shell]:rounded-none [&_.device-world-shell]:border-0 [&_.device-world-shell]:shadow-none"
        >
          <header className="flex items-center justify-between border-b border-[#22313a] bg-[#0d141a] pr-[18px] pl-[21px]">
            <div>
              <Eyebrow className="mb-[5px]">LIVE OVERVIEW</Eyebrow>
              <DialogTitle className="m-0 text-[17px] text-[#e5edf2]">设备房间总览</DialogTitle>
              <DialogDescription className="sr-only">查看所有设备房间、终端人物及当前在线状态。</DialogDescription>
            </div>
            <DialogClose asChild>
              <button aria-label="关闭设备房间总览" className="grid size-[30px] cursor-pointer place-items-center rounded-md bg-[#19212a] text-xl text-[#7c8995] transition-colors hover:text-foreground">
                <XIcon className="size-4" />
              </button>
            </DialogClose>
          </header>
          <Suspense fallback={loadingFallback}>
            <DeviceWorld
              devices={devices}
              sessions={sessions}
              busyId={busyId}
              activeSessionId={activeSessionId}
              onOpen={(device) => { setExpanded(false); onOpen(device) }}
              onProbe={onProbe}
              onEdit={(device) => { setExpanded(false); onEdit(device) }}
              onSelectTerminal={(sessionId) => { setExpanded(false); onSelectTerminal(sessionId) }}
            />
          </Suspense>
        </DialogContent>
      </Dialog>
    </>
  )
}
