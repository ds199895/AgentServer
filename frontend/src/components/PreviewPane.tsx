import { useState } from 'react'
import { ExternalLink, Maximize2, Monitor, MonitorPlay, Power, RefreshCw, Smartphone, Tablet, XIcon } from 'lucide-react'

import { api, type Preview } from '@/api'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type Props = {
  preview: Preview
  authorizedUrl: string
  onClose: () => void
  onStop: () => void
}

type Viewport = 'desktop' | 'tablet' | 'mobile'

export function PreviewPane({ preview, authorizedUrl, onClose, onStop }: Props) {
  const [frameKey, setFrameKey] = useState(0)
  const [frameUrl, setFrameUrl] = useState(authorizedUrl)
  const [viewport, setViewport] = useState<Viewport>('desktop')
  const [collapsed, setCollapsed] = useState(false)
  const sizes: Record<Viewport, string> = { desktop: '100%', tablet: '820px', mobile: '390px' }
  const refresh = () => {
    void api.previewTicket(preview.id).then(({ url }) => {
      setFrameUrl(url)
      setFrameKey((value) => value + 1)
    })
  }
  const openExternal = () => {
    const popup = window.open('', '_blank')
    void api.previewTicket(preview.id).then(({ url }) => {
      if (popup) {
        popup.opener = null
        popup.location.href = url
      } else {
        window.open(url, '_blank', 'noopener,noreferrer')
      }
    }).catch(() => popup?.close())
  }
  if (collapsed) {
    return (
      <button
        aria-label={`展开预览 ${preview.label}`}
        title={`展开预览：${preview.label}`}
        onClick={() => setCollapsed(false)}
        className="fixed right-3 bottom-3 z-40 flex cursor-pointer items-center gap-2 rounded-full border border-[#365047] bg-[#06100ee6] py-2 pr-3 pl-2.5 shadow-[0_10px_30px_#000a] backdrop-blur-[7px] transition-transform hover:scale-105 max-md:right-2 max-md:bottom-2"
      >
        <MonitorPlay className="size-3.5 flex-none text-primary" />
        <span className="max-w-[120px] truncate text-[10px] text-[#d7e6df]">{preview.label}</span>
      </button>
    )
  }
  return (
    <section className="fixed right-3 bottom-3 z-40 grid h-[min(720px,calc(100dvh-1.5rem))] w-[min(1120px,calc(100vw-1.5rem))] grid-rows-[48px_minmax(0,1fr)] overflow-hidden rounded-xl border border-[#344b53] bg-[#080d12] shadow-[0_28px_90px_#000e] max-md:right-1.5 max-md:bottom-1.5 max-md:left-1.5 max-md:h-[55dvh] max-md:w-auto">
      <header className="flex min-w-0 items-center gap-2 border-b border-[#26323c] bg-[#0d141a] px-3">
        <span className="size-2 flex-none rounded-full bg-primary shadow-[0_0_9px_#77f2b477]" />
        <div className="min-w-0 flex-1">
          <strong className="block truncate text-xs text-[#e2ebf1]">{preview.label}</strong>
          <small className="block truncate font-mono text-[8px] text-[#657581]">{preview.device_name} · localhost:{preview.target_port}</small>
        </div>
        <div role="group" aria-label="预览宽度" className="flex rounded-md border border-[#293641] bg-[#080d12] p-0.5 max-sm:hidden">
          {([
            ['desktop', Monitor], ['tablet', Tablet], ['mobile', Smartphone],
          ] as const).map(([value, Icon]) => (
            <button key={value} aria-label={`${value} 预览宽度`} onClick={() => setViewport(value)} className={cn('grid size-7 cursor-pointer place-items-center rounded text-[#697783] hover:text-[#cbd5dd]', viewport === value && 'bg-[#193025] text-primary')}><Icon className="size-3.5" /></button>
          ))}
        </div>
        <Button variant="outline" size="icon-sm" aria-label="刷新预览" title="刷新" onClick={refresh}><RefreshCw /></Button>
        <Button variant="outline" size="icon-sm" aria-label="新窗口打开预览" title="新窗口打开" onClick={openExternal}><ExternalLink /></Button>
        <Button variant="outline" size="icon-sm" aria-label="浏览器全屏" title="全屏" onClick={() => void document.documentElement.requestFullscreen?.()}><Maximize2 /></Button>
        <Button variant="destructive" size="icon-sm" aria-label="停止并关闭预览隧道" title="停止隧道" onClick={onStop}><Power /></Button>
        <Button variant="outline" size="icon-sm" aria-label="收起预览面板" title="收起面板（保留隧道）" onClick={() => setCollapsed(true)}><XIcon /></Button>
      </header>
      <div className="flex min-h-0 justify-center overflow-auto bg-[#05080b] p-2 max-md:p-0">
        <iframe
          key={frameKey}
          src={frameUrl}
          title={`${preview.label} 开发预览`}
          className="h-full min-h-[420px] max-w-full border-0 bg-white shadow-[0_0_0_1px_#26323c] max-md:min-h-0"
          style={{ width: sizes[viewport] }}
          allow="clipboard-read; clipboard-write; fullscreen"
        />
      </div>
    </section>
  )
}
