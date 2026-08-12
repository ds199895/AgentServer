import { useEffect, useRef, useState } from 'react'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import { createPortal } from 'react-dom'
import { LoaderCircle, MonitorPlay, Radio, RadioTower } from 'lucide-react'

import type { DetectedService, TerminalSession } from '@/api'
import { cn } from '@/lib/utils'

const SNAPSHOT_COMPLETE_MESSAGE = '\x01[snapshot-complete]'

type TerminalContextMenu = {
  x: number
  y: number
  hasSelection: boolean
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Fall through for HTTP origins or denied clipboard permissions.
    }
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  textarea.remove()
  if (!copied) throw new Error('clipboard unavailable')
}

type Props = {
  session: TerminalSession
  visible: boolean
  previewBusyPort?: number | null
  onPreviewService?: (service: DetectedService) => void
}

export default function TerminalPane({ session, visible, previewBusyPort, onPreviewService }: Props) {
  const sessionId = session.id
  const hostRef = useRef<HTMLDivElement>(null)
  const visibleRef = useRef(visible)
  const restoreRef = useRef<(() => void) | null>(null)
  const terminalRef = useRef<Terminal | null>(null)
  const noticeTimerRef = useRef<number | undefined>(undefined)
  const [connection, setConnection] = useState<'connecting' | 'online' | 'offline'>(
    'connecting',
  )
  const [contextMenu, setContextMenu] = useState<TerminalContextMenu | null>(null)
  const [pastePanelOpen, setPastePanelOpen] = useState(false)
  const [pasteDraft, setPasteDraft] = useState('')
  const [notice, setNotice] = useState('')
  const [showAllServices, setShowAllServices] = useState(false)
  const onlineServices = session.services.filter((service) => service.status === 'online')
  const visibleServices = showAllServices ? onlineServices : onlineServices.slice(0, 3)

  const showNotice = (message: string) => {
    setNotice(message)
    window.clearTimeout(noticeTimerRef.current)
    noticeTimerRef.current = window.setTimeout(() => setNotice(''), 1800)
  }

  const copySelection = async () => {
    const selection = terminalRef.current?.getSelection() || ''
    setContextMenu(null)
    if (!selection) {
      showNotice('请先选择终端文本')
      return
    }
    try {
      await copyText(selection)
      showNotice('已复制到剪贴板')
    } catch {
      showNotice('复制失败，请使用 Ctrl+Shift+C / Cmd+C')
    }
    terminalRef.current?.focus()
  }

  const pasteClipboard = async () => {
    setContextMenu(null)
    try {
      if (!navigator.clipboard?.readText) throw new Error('clipboard unavailable')
      const text = await navigator.clipboard.readText()
      if (!text) {
        showNotice('剪贴板中没有文本')
        return
      }
      terminalRef.current?.paste(text)
      terminalRef.current?.focus()
    } catch {
      setPasteDraft('')
      setPastePanelOpen(true)
    }
  }

  const closePastePanel = () => {
    setPastePanelOpen(false)
    setPasteDraft('')
    terminalRef.current?.focus()
  }

  const pasteDraftIntoTerminal = () => {
    if (!pasteDraft) return
    terminalRef.current?.paste(pasteDraft)
    closePastePanel()
  }

  const selectAll = () => {
    terminalRef.current?.selectAll()
    terminalRef.current?.focus()
    setContextMenu(null)
  }

  useEffect(() => {
    visibleRef.current = visible
  }, [visible])

  useEffect(() => {
    if (!hostRef.current) return

    const terminal = new Terminal({
      cursorBlink: true,
      cursorStyle: 'bar',
      fontFamily: '"SFMono-Regular", "Cascadia Code", "Liberation Mono", monospace',
      fontSize: window.innerWidth < 768 ? 12 : 14,
      lineHeight: 1.3,
      scrollback: 10_000,
      allowProposedApi: false,
      theme: {
        background: '#0b0f14',
        foreground: '#dce5ef',
        cursor: '#77f2b4',
        cursorAccent: '#0b0f14',
        selectionBackground: '#2f5f75aa',
        black: '#111821',
        red: '#ff6b7a',
        green: '#77f2b4',
        yellow: '#f7c66f',
        blue: '#78a9ff',
        magenta: '#c792ea',
        cyan: '#63d9e8',
        white: '#dce5ef',
        brightBlack: '#627180',
      },
    })
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.open(hostRef.current)
    terminalRef.current = terminal
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true
      const key = event.key.toLowerCase()
      const copyShortcut = key === 'c' && (event.metaKey || (event.ctrlKey && event.shiftKey))
      const pasteShortcut = key === 'v' && (event.metaKey || (event.ctrlKey && event.shiftKey))
      if (copyShortcut) {
        void copySelection()
        return false
      }
      if (pasteShortcut) {
        void pasteClipboard()
        return false
      }
      return true
    })
    let wheelRemainder = 0
    terminal.attachCustomWheelEventHandler((event) => {
      event.preventDefault()
      event.stopPropagation()
      const pixelsPerLine = Math.max(
        1,
        (terminal.options.fontSize ?? 14) * (terminal.options.lineHeight ?? 1),
      )
      const deltaInLines = event.deltaMode === WheelEvent.DOM_DELTA_LINE
        ? event.deltaY
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
          ? event.deltaY * terminal.rows
          : event.deltaY / pixelsPerLine
      wheelRemainder += deltaInLines
      const lines = wheelRemainder < 0 ? Math.ceil(wheelRemainder) : Math.floor(wheelRemainder)
      if (lines !== 0) {
        terminal.scrollLines(lines)
        wheelRemainder -= lines
      }
      return false
    })

    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let reconnectAttempt = 0
    let disposed = false
    let hasConnected = false
    let replayingSnapshot = true
    let restoreFrame: number | undefined
    let refreshFrame: number | undefined

    const sendSize = () => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(`\x01[${terminal.cols},${terminal.rows}]`)
      }
    }

    const fit = () => {
      if (!visibleRef.current || !hostRef.current?.clientWidth) return
      try {
        fitAddon.fit()
        sendSize()
      } catch {
        // The host may be transitioning between visible tabs.
      }
    }

    const restore = () => {
      window.cancelAnimationFrame(restoreFrame ?? 0)
      window.cancelAnimationFrame(refreshFrame ?? 0)
      restoreFrame = window.requestAnimationFrame(() => {
        fit()
        refreshFrame = window.requestAnimationFrame(() => {
          if (!visibleRef.current || disposed) return
          terminal.clearTextureAtlas()
          terminal.refresh(0, terminal.rows - 1)
          terminal.focus()
        })
      })
    }
    restoreRef.current = restore

    const connect = () => {
      if (disposed) return
      setConnection('connecting')
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/terminal/${sessionId}`)
      socket.binaryType = 'arraybuffer'
      socket.onopen = () => {
        reconnectAttempt = 0
        replayingSnapshot = true
        if (hasConnected) {
          terminal.reset()
          terminal.clear()
        }
        hasConnected = true
        setConnection('online')
        restore()
      }
      socket.onmessage = (event) => {
        if (event.data === SNAPSHOT_COMPLETE_MESSAGE) {
          // write() is queued. Its callback runs only after all snapshot bytes
          // received before this marker have finished parsing.
          terminal.write('', () => {
            replayingSnapshot = false
          })
          return
        }
        if (event.data instanceof ArrayBuffer) {
          terminal.write(new Uint8Array(event.data))
        } else if (event.data instanceof Blob) {
          void event.data.arrayBuffer().then((data) => terminal.write(new Uint8Array(data)))
        } else {
          terminal.write(String(event.data))
        }
      }
      socket.onclose = (event) => {
        setConnection('offline')
        if (disposed || event.code === 4401 || event.code === 4404) return
        const delay = Math.min(750 * 2 ** reconnectAttempt, 8000)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
      socket.onerror = () => socket?.close()
    }

    const input = terminal.onData((data) => {
      if (!replayingSnapshot && socket?.readyState === WebSocket.OPEN) {
        socket.send(new TextEncoder().encode(data))
      }
    })
    const host = hostRef.current
    const onContextMenu = (event: MouseEvent) => {
      event.preventDefault()
      setContextMenu({
        x: Math.max(8, Math.min(event.clientX, window.innerWidth - 176)),
        y: Math.max(8, Math.min(event.clientY, window.innerHeight - 126)),
        hasSelection: terminal.hasSelection(),
      })
    }
    const closeContextMenu = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest('[data-terminal-context-menu]')) {
        setContextMenu(null)
      }
    }
    const closeContextMenuOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setContextMenu(null)
        setPastePanelOpen(false)
        setPasteDraft('')
        terminal.focus()
      }
    }
    host.addEventListener('contextmenu', onContextMenu, true)
    // xterm 自身不处理触摸滚动，这里把单指拖动映射为 scrollLines，
    // 与上面的自定义滚轮逻辑保持一致（手指上滑 = 查看更新的输出）。
    let touchRemainder = 0
    let lastTouchY: number | null = null
    const onTouchStart = (event: TouchEvent) => {
      if (event.touches.length === 1) lastTouchY = event.touches[0].clientY
    }
    const onTouchMove = (event: TouchEvent) => {
      if (lastTouchY === null || event.touches.length !== 1) return
      event.preventDefault()
      const y = event.touches[0].clientY
      const pixelsPerLine = Math.max(
        1,
        (terminal.options.fontSize ?? 14) * (terminal.options.lineHeight ?? 1),
      )
      touchRemainder += (lastTouchY - y) / pixelsPerLine
      lastTouchY = y
      const lines = touchRemainder < 0 ? Math.ceil(touchRemainder) : Math.floor(touchRemainder)
      if (lines !== 0) {
        terminal.scrollLines(lines)
        touchRemainder -= lines
      }
    }
    const onTouchEnd = () => {
      lastTouchY = null
      touchRemainder = 0
    }
    host.addEventListener('touchstart', onTouchStart, { passive: true })
    host.addEventListener('touchmove', onTouchMove, { passive: false })
    host.addEventListener('touchend', onTouchEnd)
    host.addEventListener('touchcancel', onTouchEnd)
    document.addEventListener('pointerdown', closeContextMenu)
    document.addEventListener('keydown', closeContextMenuOnEscape)
    const resizeObserver = new ResizeObserver(() => window.requestAnimationFrame(fit))
    const restoreWhenPageReturns = () => {
      if (document.visibilityState === 'visible' && visibleRef.current) restore()
    }
    resizeObserver.observe(hostRef.current)
    document.addEventListener('visibilitychange', restoreWhenPageReturns)
    window.addEventListener('focus', restoreWhenPageReturns)
    window.addEventListener('pageshow', restoreWhenPageReturns)
    connect()

    return () => {
      disposed = true
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(noticeTimerRef.current)
      window.cancelAnimationFrame(restoreFrame ?? 0)
      window.cancelAnimationFrame(refreshFrame ?? 0)
      resizeObserver.disconnect()
      host.removeEventListener('contextmenu', onContextMenu, true)
      host.removeEventListener('touchstart', onTouchStart)
      host.removeEventListener('touchmove', onTouchMove)
      host.removeEventListener('touchend', onTouchEnd)
      host.removeEventListener('touchcancel', onTouchEnd)
      document.removeEventListener('pointerdown', closeContextMenu)
      document.removeEventListener('keydown', closeContextMenuOnEscape)
      document.removeEventListener('visibilitychange', restoreWhenPageReturns)
      window.removeEventListener('focus', restoreWhenPageReturns)
      window.removeEventListener('pageshow', restoreWhenPageReturns)
      input.dispose()
      socket?.close(1000)
      restoreRef.current = null
      terminalRef.current = null
      terminal.dispose()
    }
  }, [sessionId])

  useEffect(() => {
    visibleRef.current = visible
    if (visible) {
      restoreRef.current?.()
    } else {
      setContextMenu(null)
      setPastePanelOpen(false)
      setPasteDraft('')
    }
  }, [visible])

  return (
    <div className={cn(
      'absolute inset-0 pt-3.5 pr-2 pb-2 pl-3.5 max-md:pt-2.5 max-md:pr-[5px] max-md:pb-[5px] max-md:pl-[9px]',
      visible ? 'block' : 'hidden',
    )}>
      <div ref={hostRef} className="terminal-host" />
      {connection !== 'online' && (
        <div className="absolute top-3.5 right-4 flex items-center gap-[7px] rounded-md border border-[#34404b] bg-[#131a21dd] px-[9px] py-1.5 font-mono text-[9px] text-[#8b98a4]">
          <span className={cn('size-1.5 rounded-full bg-[#f0bb5a]', connection === 'offline' && 'bg-[#ed6876]')} />
          {connection === 'connecting' ? '正在连接' : '连接已断开，正在重试'}
        </div>
      )}
      {visible && onlineServices.length > 0 && (
        <aside
          aria-label="检测到的开发服务"
          className="absolute right-4 bottom-4 z-20 grid max-h-[min(310px,45%)] w-[min(320px,calc(100%-2rem))] gap-1.5 overflow-y-auto rounded-xl border border-[#30404b] bg-[#0c1319eF] p-2 shadow-[0_16px_50px_#000b] backdrop-blur-md max-md:right-2 max-md:bottom-2"
        >
          <div className="flex items-center gap-2 px-1 pb-0.5 text-[9px] font-semibold tracking-[0.09em] text-[#82919c] uppercase">
            <RadioTower className="size-3 text-primary" />开发服务 · {onlineServices.length}
          </div>
          {visibleServices.map((service) => {
            const online = service.status === 'online'
            const busy = previewBusyPort === service.port
            return (
              <div key={service.port} className="flex min-w-0 items-center gap-2 rounded-lg border border-[#25323c] bg-[#101820] px-2.5 py-2">
                <Radio className={cn(
                  'size-3.5 flex-none',
                  online && 'text-primary',
                )} />
                <div className="min-w-0 flex-1">
                  <strong className="block truncate text-[10px] text-[#dce6ed]">{service.label}</strong>
                  <small className="block truncate font-mono text-[8px] text-[#758590]">
                    localhost:{service.port} · 运行中
                  </small>
                </div>
                <button
                  type="button"
                  disabled={!online || busy}
                  onClick={() => onPreviewService?.(service)}
                  aria-label={`预览 ${service.label} localhost:${service.port}`}
                  title="一键打开安全预览"
                  className="grid h-7 flex-none cursor-pointer grid-cols-[auto_auto] items-center gap-1 rounded-md border border-[#315a48] bg-[#15251e] px-2 text-[9px] font-semibold text-primary hover:bg-[#1b3328] disabled:cursor-not-allowed disabled:border-[#2b353d] disabled:bg-[#151b20] disabled:text-[#59656e]"
                >
                  {busy ? <LoaderCircle className="size-3 animate-spin" /> : <MonitorPlay className="size-3" />}
                  {busy ? '打开中' : '预览'}
                </button>
              </div>
            )
          })}
          {onlineServices.length > 3 && (
            <button
              type="button"
              onClick={() => setShowAllServices((value) => !value)}
              className="h-7 cursor-pointer rounded-md border border-[#293641] bg-[#101820] px-2 text-[9px] text-[#84939e] hover:text-primary"
            >
              {showAllServices ? '收起' : `还有 ${onlineServices.length - 3} 个服务`}
            </button>
          )}
        </aside>
      )}
      {contextMenu && visible && createPortal(
        <div
          data-terminal-context-menu
          role="menu"
          aria-label="终端操作"
          className="fixed z-[60] grid w-[168px] gap-1 rounded-[9px] border border-[#34404b] bg-[#111820f2] p-1.5 text-[11px] text-[#c4d0d9] shadow-[0_18px_48px_#000c] backdrop-blur-md"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onContextMenu={(event) => event.preventDefault()}
        >
          <button role="menuitem" disabled={!contextMenu.hasSelection} onClick={() => void copySelection()} className="flex h-8 cursor-pointer items-center justify-between rounded-md px-2.5 text-left hover:bg-[#1b2730] hover:text-primary disabled:cursor-not-allowed disabled:opacity-40">
            <span>复制</span><kbd className="font-mono text-[8px] text-[#687985]">Ctrl+Shift+C</kbd>
          </button>
          <button role="menuitem" onClick={() => void pasteClipboard()} className="flex h-8 cursor-pointer items-center justify-between rounded-md px-2.5 text-left hover:bg-[#1b2730] hover:text-primary">
            <span>粘贴</span><kbd className="font-mono text-[8px] text-[#687985]">Ctrl+Shift+V</kbd>
          </button>
          <button role="menuitem" onClick={selectAll} className="flex h-8 cursor-pointer items-center justify-between rounded-md px-2.5 text-left hover:bg-[#1b2730] hover:text-primary">
            <span>全选</span><kbd className="font-mono text-[8px] text-[#687985]">全部缓冲区</kbd>
          </button>
        </div>,
        document.body,
      )}
      {pastePanelOpen && visible && createPortal(
        <div
          className="fixed inset-0 z-[70] grid place-items-center bg-[#03050799] p-4 backdrop-blur-[3px]"
          onPointerDown={(event) => {
            if (event.target === event.currentTarget) closePastePanel()
          }}
        >
          <form
            role="dialog"
            aria-modal="true"
            aria-labelledby={`terminal-paste-title-${sessionId}`}
            className="grid w-[min(460px,100%)] gap-3 rounded-xl border border-[#34404b] bg-[#111820f5] p-4 shadow-[0_24px_70px_#000d]"
            onSubmit={(event) => {
              event.preventDefault()
              pasteDraftIntoTerminal()
            }}
          >
            <div>
              <h2 id={`terminal-paste-title-${sessionId}`} className="m-0 text-sm font-semibold text-[#e6edf2]">粘贴到终端</h2>
              <p className="mt-1.5 mb-0 text-[10px] leading-4 text-[#7f8e99]">浏览器无法直接读取剪贴板。请在下方按 Ctrl/Cmd+V，然后确认。</p>
            </div>
            <textarea
              autoFocus
              value={pasteDraft}
              onChange={(event) => setPasteDraft(event.target.value)}
              placeholder="在此粘贴文本…"
              className="min-h-28 resize-y rounded-lg border border-[#2d3b46] bg-[#090e13] p-3 font-mono text-xs leading-5 text-[#dce5ef] outline-none placeholder:text-[#52606b] focus:border-[#47705e] focus:ring-2 focus:ring-[#77f2b41f]"
            />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={closePastePanel} className="h-8 cursor-pointer rounded-md border border-[#34404b] bg-[#151d25] px-3 text-[10px] text-[#a5b1ba] hover:bg-[#1b2630] hover:text-white">取消</button>
              <button type="submit" disabled={!pasteDraft} className="h-8 cursor-pointer rounded-md bg-primary px-3 text-[10px] font-bold text-[#07120d] hover:bg-[#9dffd0] disabled:cursor-not-allowed disabled:opacity-40">粘贴到终端</button>
            </div>
          </form>
        </div>,
        document.body,
      )}
      {notice && connection === 'online' && (
        <div className="absolute top-3.5 right-4 flex items-center gap-[7px] rounded-md border border-[#315a48] bg-[#12241ddd] px-[9px] py-1.5 font-mono text-[9px] text-primary">
          {notice}
        </div>
      )}
    </div>
  )
}
