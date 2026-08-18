import { useEffect, useRef, useState } from 'react'
import { FitAddon } from '@xterm/addon-fit'
import { WebglAddon } from '@xterm/addon-webgl'
import { Terminal } from '@xterm/xterm'
import { createPortal } from 'react-dom'
import { ChevronDown, ChevronsDown, ChevronsUp, ChevronUp, FolderOpen, Keyboard, LoaderCircle, MonitorPlay, Radio, RadioTower } from 'lucide-react'

import type { DetectedService, TerminalSession } from '@/api'
import { RunStatusBadge } from '@/components/RunStatusBadge'
import { RunTimeline } from '@/components/RunTimeline'
import { useExecutionContext } from '@/execution-context'
import { activeAgentForTerminal, activeRunForTerminal } from '@/execution-state'
import { AGENT_OUTFITS } from '@/pixel/sprites'
import { cn } from '@/lib/utils'
import {
  EMPTY_VIRTUAL_MODIFIERS,
  encodeVirtualKey,
  type VirtualKeyName,
  type VirtualModifier,
  type VirtualModifierState,
} from '@/terminal-virtual-keyboard'
import { isSnapshotProtocolReply, RecoveryInputBuffer } from '@/terminal-stream'

const SNAPSHOT_COMPLETE_MESSAGE = '\x01[snapshot-complete]'

const textEncoder = new TextEncoder()

type RestoreOptions = { rebuildAtlas?: boolean }

type TerminalContextMenu = {
  x: number
  y: number
  hasSelection: boolean
}

type VirtualKey = {
  label: string
  title: string
  key: VirtualKeyName
  className: string
}

const VIRTUAL_KEYS: VirtualKey[] = [
  { label: 'Esc', title: 'Escape', key: 'escape', className: 'col-start-1 row-start-2' },
  { label: '↑', title: '方向键上', key: 'arrowUp', className: 'col-start-2 row-start-2' },
  { label: 'Enter', title: 'Enter', key: 'enter', className: 'col-start-3 row-start-2' },
  { label: '←', title: '方向键左', key: 'arrowLeft', className: 'col-start-1 row-start-3' },
  { label: '↓', title: '方向键下', key: 'arrowDown', className: 'col-start-2 row-start-3' },
  { label: '→', title: '方向键右', key: 'arrowRight', className: 'col-start-3 row-start-3' },
  { label: 'Tab', title: 'Tab', key: 'tab', className: 'col-start-1 row-start-4 col-span-3' },
]

const VIRTUAL_MODIFIERS: Array<{ modifier: VirtualModifier; label: string }> = [
  { modifier: 'shift', label: 'Shift' },
  { modifier: 'ctrl', label: 'Ctrl' },
  { modifier: 'alt', label: 'Alt' },
]

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
  terminalLabel: string
  workspaceLabel: string
  paneNumber: number | null
  visible: boolean
  /** 当前全局聚焦的 pane；仅它可以在恢复渲染时自动聚焦 xterm。 */
  focused?: boolean
  previewBusyPort?: number | null
  onPreviewService?: (service: DetectedService) => void
  onOpenWorkspace?: () => void
}

export default function TerminalPane({
  session,
  terminalLabel,
  workspaceLabel,
  paneNumber,
  visible,
  focused = false,
  previewBusyPort,
  onPreviewService,
  onOpenWorkspace,
}: Props) {
  const sessionId = session.id
  const hostRef = useRef<HTMLDivElement>(null)
  const visibleRef = useRef(visible)
  const focusedRef = useRef(focused)
  const virtualKeyboardOpenRef = useRef(false)
  const restoreRef = useRef<((options?: RestoreOptions) => void) | null>(null)
  const stopMomentumRef = useRef<(() => void) | null>(null)
  const terminalRef = useRef<Terminal | null>(null)
  const noticeTimerRef = useRef<number | undefined>(undefined)
  const [connection, setConnection] = useState<'connecting' | 'online' | 'offline'>(
    'connecting',
  )
  const [recovering, setRecovering] = useState(true)
  const [contextMenu, setContextMenu] = useState<TerminalContextMenu | null>(null)
  const [pastePanelOpen, setPastePanelOpen] = useState(false)
  const [virtualKeyboardOpen, setVirtualKeyboardOpen] = useState(false)
  const [virtualModifiers, setVirtualModifiers] = useState<VirtualModifierState>(
    EMPTY_VIRTUAL_MODIFIERS,
  )
  const [pasteDraft, setPasteDraft] = useState('')
  const [notice, setNotice] = useState('')
  const [showAllServices, setShowAllServices] = useState(false)
  const [scrolledUp, setScrolledUp] = useState(false)
  const [timelineRunId, setTimelineRunId] = useState<string | null>(null)
  const [servicesCollapsed, setServicesCollapsed] = useState(
    () => window.matchMedia('(max-width: 767px), (pointer: coarse)').matches,
  )
  const onlineServices = session.services.filter((service) => service.status === 'online')
  const visibleServices = showAllServices ? onlineServices : onlineServices.slice(0, 3)
  const deviceLabel = session.device_name || '本机'
  const execution = useExecutionContext()
  const executionRun = activeRunForTerminal(execution.snapshot, session.id)
  const executionAgent = activeAgentForTerminal(execution.snapshot, session.id)
  const timelineRun = timelineRunId
    ? execution.snapshot?.runs.find((run) => run.run_id === timelineRunId) ?? null
    : null
  const agentKind = executionAgent?.kind || executionRun?.agent_kind || session.agent?.kind || null
  const agentName = agentKind ? (AGENT_OUTFITS[agentKind]?.name ?? agentKind) : null
  const connectionLabel = !session.active
    ? '进程已退出'
    : connection === 'online' && recovering
      ? '正在恢复终端'
      : connection === 'online'
        ? '已连接'
        : connection === 'connecting'
          ? '正在连接'
          : '连接已断开，正在重试'

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

  const toggleVirtualKeyboard = () => {
    const next = !virtualKeyboardOpenRef.current
    virtualKeyboardOpenRef.current = next
    // xterm focuses its hidden textarea. Blurring it before opening the
    // accessory panel prevents mobile browsers from treating this click as a
    // request to show their own IME. A later explicit tap on the terminal can
    // still bring the native keyboard back when text input is wanted.
    if (next) terminalRef.current?.blur()
    setVirtualKeyboardOpen(next)
    if (!next) setVirtualModifiers(EMPTY_VIRTUAL_MODIFIERS)
  }

  const toggleVirtualModifier = (modifier: VirtualModifier) => {
    setVirtualModifiers((current) => ({ ...current, [modifier]: !current[modifier] }))
  }

  const sendVirtualKey = (key: VirtualKey) => {
    const terminal = terminalRef.current
    if (!terminal) return
    terminal.input(
      encodeVirtualKey(key.key, virtualModifiers, terminal.modes.applicationCursorKeysMode),
      true,
    )
    setVirtualModifiers(EMPTY_VIRTUAL_MODIFIERS)
  }

  const selectAll = () => {
    terminalRef.current?.selectAll()
    terminalRef.current?.focus()
    setContextMenu(null)
  }

  useEffect(() => {
    visibleRef.current = visible
    focusedRef.current = focused
  }, [visible, focused])

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
    // xterm 6 ships only the DOM renderer, which builds a span per style run per
    // row. The WebGL addon registers itself during open()'s onWillOpen, so it has
    // to be loaded first; if construction or context creation fails, xterm falls
    // back to the DOM renderer on its own.
    let webglAddon: WebglAddon | null = null
    try {
      // Hidden tabs keep their canvas. Preserving its drawing buffer avoids a
      // blank first frame when Chromium discards an occluded WebGL surface.
      const addon = new WebglAddon(true)
      addon.onContextLoss(() => {
        addon.dispose()
        if (webglAddon === addon) webglAddon = null
      })
      terminal.loadAddon(addon)
      webglAddon = addon
    } catch {
      webglAddon = null
    }
    terminal.open(hostRef.current)
    terminalRef.current = terminal
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true
      const key = event.key.toLowerCase()
      const copyShortcut = key === 'c' && (event.metaKey || (event.ctrlKey && event.shiftKey))
      const pasteShortcut = key === 'v' && (event.metaKey || (event.ctrlKey && event.shiftKey))
      if (copyShortcut) {
        // xterm 对返回 false 仅停止内部处理,不取消浏览器默认行为。
        // Linux 上 Ctrl+Shift+C 默认打开 DevTools 审查模式,必须自行 preventDefault。
        event.preventDefault()
        void copySelection()
        return false
      }
      if (pasteShortcut) {
        // Linux 上 Ctrl+Shift+V 是浏览器「以纯文本粘贴」默认行为,会在 xterm 的
        // textarea 上再派发一次 paste 事件(xterm 内部也监听了它),导致粘贴两次。
        // preventDefault 取消默认行为,只走下方 readText 这一条路径。
        event.preventDefault()
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
    const recoveryInput = new RecoveryInputBuffer()
    let recoveryOverflowReported = false
    let restoreFrame: number | undefined
    let refreshFrame: number | undefined
    let resizeFrame: number | undefined

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

    const restore = ({ rebuildAtlas = false }: RestoreOptions = {}) => {
      window.cancelAnimationFrame(restoreFrame ?? 0)
      window.cancelAnimationFrame(refreshFrame ?? 0)
      restoreFrame = window.requestAnimationFrame(() => {
        fit()
        refreshFrame = window.requestAnimationFrame(() => {
          if (!visibleRef.current || disposed) return
          // The WebGL texture atlas can become invalid while Chromium occludes
          // a hidden tab or after the OS resumes from sleep. Rebuild it only at
          // those explicit recovery boundaries.
          // clearTextureAtlas already requests a full refresh in xterm. Calling
          // refresh as well would enqueue a second full-viewport paint.
          if (rebuildAtlas) terminal.clearTextureAtlas()
          else terminal.refresh(0, terminal.rows - 1)
          // ResizeObserver、WS onopen 和 visibilitychange 都可能在用户已经
          // 聚焦分隔条/按钮后触发。只允许当前 pane 在页面没有交互焦点，或
          // 焦点本来就在自己的 xterm 内时恢复焦点，避免键盘操作被异步抢走。
          const activeElement = document.activeElement
          const mayRestoreFocus =
            !activeElement ||
            activeElement === document.body ||
            activeElement === document.documentElement ||
            hostRef.current?.contains(activeElement)
          if (focusedRef.current && !virtualKeyboardOpenRef.current && mayRestoreFocus) {
            terminal.focus()
          }
        })
      })
    }
    restoreRef.current = restore

    const sendInput = (data: Uint8Array<ArrayBuffer>) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(data)
    }

    const flushRecoveryInput = () => {
      for (const data of recoveryInput.drain()) sendInput(data)
      recoveryOverflowReported = false
    }

    const connect = () => {
      if (disposed) return
      setConnection('connecting')
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/terminal/${sessionId}`)
      socket.binaryType = 'arraybuffer'
      socket.onopen = () => {
        reconnectAttempt = 0
        replayingSnapshot = true
        recoveryOverflowReported = false
        setRecovering(true)
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
            setRecovering(false)
            flushRecoveryInput()
            restore({ rebuildAtlas: true })
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
        replayingSnapshot = true
        setConnection('offline')
        setRecovering(true)
        if (disposed || event.code === 4401 || event.code === 4404) return
        const delay = Math.min(750 * 2 ** reconnectAttempt, 8000)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
      socket.onerror = () => socket?.close()
    }

    const input = terminal.onData((data) => {
      if (replayingSnapshot) {
        if (isSnapshotProtocolReply(data)) return
        if (!recoveryInput.push(data) && !recoveryOverflowReported) {
          recoveryOverflowReported = true
          showNotice('终端恢复期间输入过多，已停止继续缓存')
        }
        return
      }
      sendInput(textEncoder.encode(data))
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
    // xterm 自身不处理触摸滚动。这里把单指拖动按像素平滑映射为 scrollLines，
    // 松手后根据滑动速度做惯性衰减，模拟原生滚动列表的手感。
    let touchRemainder = 0
    let lastTouchY: number | null = null
    let lastTouchTime = 0
    let touchVelocity = 0
    let momentumFrame: number | undefined
    const scrollPixels = (pixels: number) => {
      const pixelsPerLine = Math.max(
        1,
        (terminal.options.fontSize ?? 14) * (terminal.options.lineHeight ?? 1),
      )
      touchRemainder += pixels / pixelsPerLine
      const lines = touchRemainder < 0 ? Math.ceil(touchRemainder) : Math.floor(touchRemainder)
      if (lines !== 0) {
        terminal.scrollLines(lines)
        touchRemainder -= lines
      }
    }
    const stopMomentum = () => {
      if (momentumFrame !== undefined) window.cancelAnimationFrame(momentumFrame)
      momentumFrame = undefined
      touchVelocity = 0
    }
    stopMomentumRef.current = stopMomentum
    const onTouchStart = (event: TouchEvent) => {
      stopMomentum()
      if (event.touches.length === 1) {
        lastTouchY = event.touches[0].clientY
        lastTouchTime = performance.now()
      }
    }
    const onTouchMove = (event: TouchEvent) => {
      if (lastTouchY === null || event.touches.length !== 1) return
      event.preventDefault()
      const now = performance.now()
      const y = event.touches[0].clientY
      const delta = lastTouchY - y
      const elapsed = now - lastTouchTime
      if (elapsed > 0) {
        // 指数平滑瞬时速度（px/ms），过滤个别触摸事件的抖动
        touchVelocity = touchVelocity * 0.6 + (delta / elapsed) * 0.4
      }
      lastTouchY = y
      lastTouchTime = now
      scrollPixels(delta)
    }
    const onTouchEnd = () => {
      lastTouchY = null
      if (Math.abs(touchVelocity) < 0.06) {
        touchVelocity = 0
        return
      }
      let previous = performance.now()
      const step = () => {
        const now = performance.now()
        const elapsed = Math.min(now - previous, 48)
        previous = now
        touchVelocity *= Math.pow(0.94, elapsed / 16.7)
        if (Math.abs(touchVelocity) < 0.02) {
          stopMomentum()
          return
        }
        scrollPixels(touchVelocity * elapsed)
        momentumFrame = window.requestAnimationFrame(step)
      }
      momentumFrame = window.requestAnimationFrame(step)
    }
    // 跟踪滚动位置，用于显示「直达顶部 / 直达底部」按钮
    const updateScrollState = () => {
      const buffer = terminal.buffer.active
      setScrolledUp(buffer.viewportY < buffer.baseY)
    }
    const scrollListener = terminal.onScroll(updateScrollState)
    host.addEventListener('touchstart', onTouchStart, { passive: true })
    host.addEventListener('touchmove', onTouchMove, { passive: false })
    host.addEventListener('touchend', onTouchEnd)
    host.addEventListener('touchcancel', onTouchEnd)
    document.addEventListener('pointerdown', closeContextMenu)
    document.addEventListener('keydown', closeContextMenuOnEscape)
    const resizeObserver = new ResizeObserver(() => {
      window.cancelAnimationFrame(resizeFrame ?? 0)
      resizeFrame = window.requestAnimationFrame(() => {
        if (visibleRef.current) restore()
      })
    })
    // xterm has its own IntersectionObserver and pauses rendering while a tab
    // uses display:none. Register after terminal.open(), so this callback runs
    // after xterm observes the reveal and schedules a reliable recovery paint.
    const revealObserver = new IntersectionObserver((entries) => {
      const entry = entries[entries.length - 1]
      if (entry?.isIntersecting && visibleRef.current) {
        restore({ rebuildAtlas: true })
      }
    })
    const restoreWhenPageReturns = () => {
      if (document.visibilityState === 'visible' && visibleRef.current) {
        restore({ rebuildAtlas: true })
      }
    }
    resizeObserver.observe(hostRef.current)
    revealObserver.observe(hostRef.current)
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
      window.cancelAnimationFrame(resizeFrame ?? 0)
      resizeObserver.disconnect()
      revealObserver.disconnect()
      stopMomentum()
      scrollListener.dispose()
      stopMomentumRef.current = null
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
      recoveryInput.clear()
      socket?.close(1000)
      restoreRef.current = null
      terminalRef.current = null
      // Release the WebGL context before the terminal so the addon does not
      // touch an already-disposed renderer.
      webglAddon?.dispose()
      webglAddon = null
      terminal.dispose()
    }
  }, [sessionId])

  useEffect(() => {
    visibleRef.current = visible
    focusedRef.current = focused
    if (visible) {
      restoreRef.current?.({ rebuildAtlas: true })
    } else {
      setContextMenu(null)
      setPastePanelOpen(false)
      virtualKeyboardOpenRef.current = false
      setVirtualKeyboardOpen(false)
      setVirtualModifiers(EMPTY_VIRTUAL_MODIFIERS)
      setPasteDraft('')
    }
  }, [visible, focused])

  return (
    <div className={cn(
      'absolute inset-0 min-h-0 min-w-0 grid-rows-[30px_minmax(0,1fr)] max-md:grid-rows-[28px_minmax(0,1fr)]',
      visible ? 'grid' : 'hidden',
    )} data-terminal-recovering={recovering ? 'true' : 'false'}>
      <header
        aria-label={`${deviceLabel} ${terminalLabel} 终端信息`}
        className={cn(
          'relative z-20 flex min-w-0 items-center gap-1.5 overflow-hidden border-b border-[#202b35] bg-[#0d1319] px-2 text-[9px] text-[#84939e]',
          focused && 'border-[#315a48] bg-[#102019]',
        )}
      >
        <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden">
          <span
            role="status"
            aria-label={connectionLabel}
            title={connectionLabel}
            className="flex flex-none items-center gap-1.5"
          >
            <i
              aria-hidden="true"
              className={cn(
                'size-1.5 flex-none rounded-full bg-[#f0bb5a] shadow-[0_0_7px_#f0bb5a66]',
                connection === 'online' && session.active && 'bg-primary shadow-[0_0_8px_#77f2b488]',
                connection === 'offline' && 'bg-[#ed6876] shadow-[0_0_8px_#ed687666]',
                !session.active && 'bg-[#7f5960] shadow-none',
              )}
            />
            <span className={cn(
              'font-medium text-[#8e9ba5] max-md:sr-only',
              connection === 'online' && session.active && 'text-[#8fd5b3]',
              connection === 'offline' && 'text-[#e58b94]',
              !session.active && 'text-[#a7848a]',
            )}>
              {connectionLabel}
            </span>
          </span>
          {paneNumber !== null && (
            <span className="flex-none rounded border border-[#2b3741] bg-[#111920] px-1 py-0.5 font-mono text-[7px] text-[#9caab4]">
              P{paneNumber}
            </span>
          )}
          <strong className="max-w-[24%] flex-none truncate text-[10px] font-semibold text-[#d7e1e8]" title={deviceLabel}>
            {deviceLabel}
          </strong>
          <span aria-hidden="true" className="flex-none text-[#44515b]">/</span>
          <strong className="min-w-0 truncate text-[10px] font-semibold text-[#edf4f9]" title={terminalLabel}>
            {terminalLabel}
          </strong>
          <code className="flex-none font-mono text-[8px] text-[#657581]" title={session.id}>
            {session.id.slice(0, 6)}
          </code>
          <span aria-hidden="true" className="flex-none text-[#44515b]">·</span>
          <span className="min-w-0 truncate font-mono text-[8px] text-[#667782]" title={workspaceLabel}>
            {workspaceLabel}
          </span>
          {(executionRun || executionAgent) && (
            <RunStatusBadge
              run={executionRun}
              agent={executionAgent}
              compact
              onClick={executionRun ? () => setTimelineRunId(executionRun.run_id) : undefined}
            />
          )}
          {!executionRun && !executionAgent && agentName && (
            <span
              className="flex-none rounded border border-[#2b3741] bg-[#111920] px-1 py-0.5 font-mono text-[7px] text-[#9caab4]"
              title={session.agent?.cwd ? `${agentName} · ${session.agent.cwd}` : agentName}
            >
              {agentName}
            </span>
          )}
          <button
            type="button"
            onClick={onOpenWorkspace}
            aria-label="打开工作区文件"
            title="打开文件与 Artifacts"
            className="ml-auto flex h-5 flex-none cursor-pointer items-center gap-1 rounded border border-[#2b3842] bg-[#111920] px-1.5 font-semibold text-[#8d9aa5] transition-colors hover:border-[#315a48] hover:bg-[#15251e] hover:text-primary"
          >
            <FolderOpen className="size-3" />
            <span className="max-lg:hidden">文件</span>
          </button>
        </div>
      </header>
      <div className="relative min-h-0 min-w-0 overflow-hidden pt-2 pr-2 pb-2 pl-3.5 max-md:pt-1.5 max-md:pr-[5px] max-md:pb-[5px] max-md:pl-[9px]">
        <div ref={hostRef} className="terminal-host" />
      {visible && scrolledUp && (
        <>
          <button
            type="button"
            aria-label="直达底部"
            title="直达底部"
            onClick={() => {
              stopMomentumRef.current?.()
              terminalRef.current?.scrollToBottom()
              terminalRef.current?.focus()
            }}
            className="absolute top-2 left-4 z-20 grid size-8 cursor-pointer place-items-center rounded-full border border-[#2d3b46] bg-[#101820cc] text-[#9aa9b4] shadow-[0_8px_24px_#0009] backdrop-blur-md transition-colors hover:border-[#315a48] hover:text-primary max-md:top-1.5 max-md:left-2.5 max-md:size-7"
          >
            <ChevronsDown className="size-4 max-md:size-3.5" />
          </button>
          <button
            type="button"
            aria-label="直达顶部"
            title="直达顶部"
            onClick={() => {
              stopMomentumRef.current?.()
              terminalRef.current?.scrollToTop()
              terminalRef.current?.focus()
            }}
            className="absolute bottom-4 left-4 z-20 grid size-8 cursor-pointer place-items-center rounded-full border border-[#2d3b46] bg-[#101820cc] text-[#9aa9b4] shadow-[0_8px_24px_#0009] backdrop-blur-md transition-colors hover:border-[#315a48] hover:text-primary max-md:bottom-2 max-md:left-2.5 max-md:size-7"
          >
            <ChevronsUp className="size-4 max-md:size-3.5" />
          </button>
        </>
      )}
      {visible && (
        <div
          className={cn(
            'absolute right-4 z-30 grid justify-items-end gap-2 max-md:right-2',
            onlineServices.length > 0 ? 'bottom-16 max-md:bottom-11' : 'bottom-4 max-md:bottom-2',
            !servicesCollapsed && onlineServices.length > 0 && 'max-md:bottom-[calc(30%+1rem)]',
          )}
        >
          {virtualKeyboardOpen && (
            <div
              role="group"
              aria-label="终端辅助键盘"
              className="grid w-[min(236px,calc(100vw-1rem))] grid-cols-3 grid-rows-4 gap-1.5 rounded-xl border border-[#30404b] bg-[#0c1319ef] p-2 shadow-[0_16px_50px_#000b] backdrop-blur-md max-md:w-[min(218px,calc(100vw-1rem))] max-md:gap-1 max-md:rounded-lg max-md:p-1.5"
            >
              {VIRTUAL_MODIFIERS.map(({ modifier, label }) => {
                const active = virtualModifiers[modifier]
                return (
                  <button
                    key={modifier}
                    type="button"
                    aria-label={`${label}组合键`}
                    aria-pressed={active}
                    title={active ? `取消 ${label}` : `与下一个按键组合`}
                    onPointerDown={(event) => event.preventDefault()}
                    onClick={() => toggleVirtualModifier(modifier)}
                    className={cn(
                      'col-span-1 row-start-1 grid h-9 cursor-pointer place-items-center rounded-md border border-[#2d3b46] bg-[#101820] px-2 font-mono text-[11px] font-semibold text-[#c4d0d9] shadow-[inset_0_-1px_0_#0006] transition-colors hover:border-[#315a48] hover:bg-[#15251e] hover:text-primary active:translate-y-px max-md:h-8 max-md:text-[10px]',
                      active && 'border-[#58a982] bg-[#193025] text-primary shadow-[inset_0_0_0_1px_#77f2b433]',
                    )}
                  >
                    {label}
                  </button>
                )
              })}
              {VIRTUAL_KEYS.map((key) => (
                <button
                  key={key.label}
                  type="button"
                  aria-label={key.title}
                  title={key.title}
                  onPointerDown={(event) => event.preventDefault()}
                  onClick={() => sendVirtualKey(key)}
                  className={cn(
                    'grid h-9 cursor-pointer place-items-center rounded-md border border-[#2d3b46] bg-[#101820] px-2 font-mono text-[11px] font-semibold text-[#c4d0d9] shadow-[inset_0_-1px_0_#0006] transition-colors hover:border-[#315a48] hover:bg-[#15251e] hover:text-primary active:translate-y-px max-md:h-8 max-md:text-[10px]',
                    key.className,
                  )}
                >
                  {key.label}
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            aria-label={virtualKeyboardOpen ? '收起虚拟键盘' : '打开虚拟键盘'}
            aria-pressed={virtualKeyboardOpen}
            title={virtualKeyboardOpen ? '收起虚拟键盘' : '打开虚拟键盘'}
            onPointerDown={(event) => event.preventDefault()}
            onClick={toggleVirtualKeyboard}
            className={cn(
              'grid size-9 cursor-pointer place-items-center rounded-full border border-[#2d3b46] bg-[#101820cc] text-[#9aa9b4] shadow-[0_10px_30px_#000a] backdrop-blur-md transition-colors hover:border-[#315a48] hover:text-primary max-md:size-8',
              virtualKeyboardOpen && 'border-[#315a48] bg-[#15251e] text-primary',
            )}
          >
            <Keyboard className="size-4 max-md:size-3.5" />
          </button>
        </div>
      )}
      {visible && onlineServices.length > 0 && servicesCollapsed && (
        <button
          type="button"
          aria-label="展开开发服务"
          title="展开开发服务"
          onClick={() => setServicesCollapsed(false)}
          className="absolute right-4 bottom-4 z-20 flex h-8 cursor-pointer items-center gap-1.5 rounded-lg border border-[#315a48] bg-[#102019ed] px-2.5 text-[9px] font-semibold text-primary shadow-[0_10px_30px_#000a] backdrop-blur-md hover:bg-[#193025] max-md:right-2 max-md:bottom-2 max-md:h-7 max-md:px-2"
        >
          <RadioTower className="size-3" />
          服务 {onlineServices.length}
          <ChevronUp className="size-3" />
        </button>
      )}
      {visible && onlineServices.length > 0 && !servicesCollapsed && (
        <aside
          aria-label="检测到的开发服务"
          className="absolute right-4 bottom-4 z-20 grid max-h-[min(310px,45%)] w-[min(300px,calc(100%-2rem))] gap-1.5 overflow-y-auto rounded-xl border border-[#30404b] bg-[#0c1319eF] p-2 shadow-[0_16px_50px_#000b] backdrop-blur-md max-md:right-2 max-md:bottom-2 max-md:max-h-[min(180px,30%)] max-md:w-[min(230px,62vw)] max-md:gap-1 max-md:rounded-lg max-md:p-1.5"
        >
          <div className="flex items-center gap-2 px-1 pb-0.5 text-[9px] font-semibold tracking-[0.09em] text-[#82919c] uppercase max-md:gap-1.5">
            <RadioTower className="size-3 text-primary" />
            <span className="min-w-0 flex-1 truncate">开发服务 · {onlineServices.length}</span>
            <button
              type="button"
              aria-label="收起开发服务"
              title="收起开发服务"
              onClick={() => setServicesCollapsed(true)}
              className="grid size-6 flex-none cursor-pointer place-items-center rounded-md text-[#71818c] hover:bg-[#1a2730] hover:text-primary max-md:size-5"
            >
              <ChevronDown className="size-3.5" />
            </button>
          </div>
          {visibleServices.map((service) => {
            const online = service.status === 'online'
            const busy = previewBusyPort === service.port
            return (
              <div key={service.port} className="flex min-w-0 items-center gap-2 rounded-lg border border-[#25323c] bg-[#101820] px-2.5 py-2 max-md:gap-1.5 max-md:rounded-md max-md:px-2 max-md:py-1.5">
                <Radio className={cn(
                  'size-3.5 flex-none max-md:hidden',
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
                  className="grid h-7 flex-none cursor-pointer grid-cols-[auto_auto] items-center gap-1 rounded-md border border-[#315a48] bg-[#15251e] px-2 text-[9px] font-semibold text-primary hover:bg-[#1b3328] disabled:cursor-not-allowed disabled:border-[#2b353d] disabled:bg-[#151b20] disabled:text-[#59656e] max-md:size-7 max-md:grid-cols-1 max-md:px-0"
                >
                  {busy ? <LoaderCircle className="size-3 animate-spin" /> : <MonitorPlay className="size-3" />}
                  <span className="max-md:hidden">{busy ? '打开中' : '预览'}</span>
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
        <div className="absolute top-2 right-3 flex items-center gap-[7px] rounded-md border border-[#315a48] bg-[#12241ddd] px-[9px] py-1.5 font-mono text-[9px] text-primary max-md:top-1.5 max-md:right-2">
          {notice}
        </div>
      )}
      {execution.snapshot && timelineRun && (
        <RunTimeline
          snapshot={execution.snapshot}
          run={timelineRun}
          open
          onOpenChange={(open) => { if (!open) setTimelineRunId(null) }}
        />
      )}
      </div>
    </div>
  )
}
