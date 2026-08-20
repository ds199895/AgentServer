import { useCallback, useEffect, useRef, useState } from 'react'
import { XIcon } from 'lucide-react'

import { api, frontendBuildSha, normalizeTerminalSessions, type DetectedService, type Device, type Preview, type TerminalSession } from '@/api'
import { DeviceDashboard } from '@/components/DeviceDashboard'
import { DeviceDialog } from '@/components/DeviceDialog'
import { DeviceRuntimeDialog } from '@/components/DeviceRuntimeDialog'
import { DownloadsPage } from '@/components/DownloadsPage'
import { Eyebrow } from '@/components/Eyebrow'
import { ExecutionStatusNotice } from '@/components/ExecutionStatusNotice'
import { Login } from '@/components/Login'
import { PasswordDialog } from '@/components/PasswordDialog'
import { PreviewDialog } from '@/components/PreviewDialog'
import { PreviewPane } from '@/components/PreviewPane'
import { TerminalEmpty } from '@/components/TerminalEmpty'
import { TerminalGrid } from '@/components/TerminalGrid'
import { TerminalRoomOverview } from '@/components/TerminalRoomOverview'
import { TerminalTabsBar } from '@/components/TerminalTabsBar'
import { Topbar, type MainPage } from '@/components/Topbar'
import { WorkspacePane } from '@/components/WorkspacePane'
import {
  activateSession,
  closeLeaf,
  createLeaf,
  detachSession,
  findLeaf,
  firstLeaf,
  leafOfSession,
  listLeaves,
  moveSession,
  parseLayout,
  pinSession,
  previewSession,
  reconcile,
  removeSession,
  serializeLayout,
  setRatio,
  splitLeaf,
  type LayoutDirection,
  type LayoutNode,
} from '@/terminal-layout'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ExecutionProvider } from '@/execution-context'
import { activeAgentForTerminal, evidenceFreshness, fieldEvidence } from '@/execution-state'
import { cn } from '@/lib/utils'
import { useExecutionStream } from '@/useExecutionStream'

const LAST_TERMINAL_KEY = 'agentserver:last-terminal-id'
const LAYOUT_KEY = 'agentserver:terminal-layout-v1'
const SINGLE_PANE_MEDIA_QUERY = '(max-width: 767px), (pointer: coarse) and (orientation: portrait)'

type ShowTerminalOptions = {
  replace?: boolean
  /** preview 可替换；pinned 持久；select 仅切换 pane 内既有标签。 */
  disposition?: 'preview' | 'pinned' | 'select'
  /** 键盘在 TabStrip 中导航时保留 Tab 焦点，不抢回 xterm。 */
  focusInput?: boolean
  /** 聚焦空 Pane 时保留“最近终端”仅供用户后续显式 reveal。 */
  preserveLast?: boolean
  /** 打开操作的目标窗格；已固定在其他窗格的会话仍按 reveal 规则处理。 */
  targetLeafId?: string | null
}

function initialLayout(): { layout: LayoutNode | null; focusedLeafId: string | null } {
  try {
    const persisted = parseLayout(window.localStorage.getItem(LAYOUT_KEY))
    if (persisted) return { layout: persisted.layout, focusedLeafId: persisted.focusedLeafId }
  } catch {
    // Storage unavailable — fall through to an empty layout.
  }
  return { layout: null, focusedLeafId: null }
}

function isCoarseLayout(): boolean {
  return window.matchMedia(SINGLE_PANE_MEDIA_QUERY).matches
}

// 与 pixel/sprites.ts 的 AGENT_OUTFITS 名称保持一致;这里用小表避免把
// 像素图模块拉进主包。
const AGENT_DISPLAY_NAMES: Record<string, string> = {
  codex: 'Codex',
  claude: 'Claude',
  kimi: 'Kimi',
  deepseek: 'DeepSeek',
}

function agentDisplayName(kind: string): string {
  return AGENT_DISPLAY_NAMES[kind] ?? kind
}

type AgentSwitchTarget = {
  sessionId: string
  kind: string
  cwd: string
  relPath: string
}

function storedTerminalId(): string | null {
  try {
    return window.localStorage.getItem(LAST_TERMINAL_KEY)
  } catch {
    return null
  }
}

function storeTerminalId(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(LAST_TERMINAL_KEY, id)
    else window.localStorage.removeItem(LAST_TERMINAL_KEY)
  } catch {
    // Routing still works when browser storage is unavailable.
  }
}

function focusTerminalInput(id: string): void {
  const previousActiveElement = document.activeElement
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    const activeElement = document.activeElement
    const focusIsStillAvailable =
      activeElement === previousActiveElement ||
      activeElement === document.body ||
      activeElement === document.documentElement ||
      (previousActiveElement instanceof Element && !previousActiveElement.isConnected)
    if (!focusIsStillAvailable) return
    const pane = document.querySelector<HTMLElement>(
      `[data-terminal-id="${CSS.escape(id)}"]`,
    )
    pane?.querySelector<HTMLElement>('.xterm-helper-textarea')?.focus({ preventScroll: true })
  }))
}

function routeFromLocation(): { page: MainPage; terminalId: string | null } {
  const terminalMatch = window.location.pathname.match(/^\/terminal\/([^/]+)\/?$/)
  if (terminalMatch) {
    try {
      return { page: 'terminals', terminalId: decodeURIComponent(terminalMatch[1]) }
    } catch {
      return { page: 'terminals', terminalId: terminalMatch[1] }
    }
  }
  if (/^\/terminals\/?$/.test(window.location.pathname)) {
    return { page: 'terminals', terminalId: null }
  }
  if (/^\/setup\/?$/.test(window.location.pathname)) {
    return { page: 'setup', terminalId: null }
  }
  return { page: 'devices', terminalId: null }
}

export default function App() {
  const [username, setUsername] = useState<string | null | undefined>(undefined)
  const [devices, setDevices] = useState<Device[]>([])
  const [sessions, setSessions] = useState<TerminalSession[]>([])
  const [previews, setPreviews] = useState<Preview[]>([])
  const [activeId, setActiveId] = useState<string | null>(() => routeFromLocation().terminalId)
  const [page, setPage] = useState<MainPage>(() => routeFromLocation().page)
  const [missingTerminalId, setMissingTerminalId] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [deviceDialog, setDeviceDialog] = useState<Device | 'new' | null>(null)
  const [runtimeDevice, setRuntimeDevice] = useState<Device | null>(null)
  const [previewTarget, setPreviewTarget] = useState<{ deviceId?: string; terminalId?: string } | null>(null)
  const [activePreview, setActivePreview] = useState<{ preview: Preview; url: string } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [cloningId, setCloningId] = useState<string | null>(null)
  const [closeTarget, setCloseTarget] = useState<TerminalSession | null>(null)
  const [previewBusy, setPreviewBusy] = useState<{ terminalId: string; port: number } | null>(null)
  const [workspaceSessionId, setWorkspaceSessionId] = useState<string | null>(null)
  const [agentSwitchTarget, setAgentSwitchTarget] = useState<AgentSwitchTarget | null>(null)
  const [workspaceFocusPath, setWorkspaceFocusPath] = useState<string | null>(null)
  // initialLayout() parses localStorage; hold it in one state slot so bootstrap
  // validates the persisted layout once instead of once per derived field.
  const [persistedBoot] = useState(initialLayout)
  const [layout, setLayout] = useState<LayoutNode | null>(persistedBoot.layout)
  const [focusedLeafId, setFocusedLeafId] = useState<string | null>(persistedBoot.focusedLeafId)
  const [forceSingle, setForceSingle] = useState(isCoarseLayout)
  const [focusMode, setFocusMode] = useState(false)
  const [error, setError] = useState('')
  const [startupError, setStartupError] = useState('')
  const execution = useExecutionStream(Boolean(username))
  const activeIdRef = useRef<string | null>(routeFromLocation().terminalId)
  const lastTerminalIdRef = useRef<string | null>(routeFromLocation().terminalId || storedTerminalId())
  const activePreviewRef = useRef<{ preview: Preview; url: string } | null>(null)
  const layoutRef = useRef<LayoutNode | null>(layout)
  const focusedLeafIdRef = useRef<string | null>(focusedLeafId)
  const cloningRef = useRef(false)
  const loadRequestIdRef = useRef(0)
  const sessionMutationEpochRef = useRef(0)
  const sessionMutationDepthRef = useRef(0)
  // 轮询每 5 秒都会返回全新的数组/对象。无条件 setState 会让下游所有
  // useMemo 失效并重渲染每个已挂载的 pane,即使数据一个字节都没变。
  // 一次 stringify 远比一轮全树重渲染便宜。
  const devicesSignatureRef = useRef('')
  const sessionsSignatureRef = useRef('')
  const previewsSignatureRef = useRef('')
  const sessionsPushHealthyRef = useRef(false)
  // sessionId → 已提示过的 agent cwd;同一路径只问一次,换目录再问。
  const agentPromptedRef = useRef(new Map<string, string>())

  // 布局操作需要同步读取最新值(轮询 load 与事件处理器都会触发),
  // 因此通过 ref 镜像 + 成对 setter,避免在 setState updater 里产生副作用。
  const setLayoutState = (next: LayoutNode | null) => {
    layoutRef.current = next
    setLayout(next)
  }
  const setFocusedLeafState = (next: string | null) => {
    focusedLeafIdRef.current = next
    setFocusedLeafId(next)
  }

  // 任何本地乐观更新都会让指纹与实际 state 脱钩。清空后下一轮轮询
  // 一定重新落盘,不会因为指纹陈旧而把过期 state 留死。
  const invalidateSnapshots = () => {
    devicesSignatureRef.current = ''
    sessionsSignatureRef.current = ''
    previewsSignatureRef.current = ''
  }

  const beginSessionMutation = () => {
    sessionMutationDepthRef.current += 1
    sessionMutationEpochRef.current += 1
    invalidateSnapshots()
  }
  const finishSessionMutation = () => {
    sessionMutationDepthRef.current = Math.max(0, sessionMutationDepthRef.current - 1)
    sessionMutationEpochRef.current += 1
  }

  // 会话快照有两个来源:HTTP 轮询与 /ws/sessions 推送。两者共用同一套
  // 对账逻辑,以免路由/布局同步出现两份实现。
  const applySessions = useCallback((nextSessions: TerminalSession[]) => {
    // 只跳过 setState;下面的路由/布局对账必须照常执行,它还负责 URL 同步,
    // 而布局可能在两次快照之间被用户改动过。
    const sessionsSignature = JSON.stringify(nextSessions)
    if (sessionsSignature !== sessionsSignatureRef.current) {
      sessionsSignatureRef.current = sessionsSignature
      setSessions(nextSessions)
    }
    const route = routeFromLocation()
    const previousLayout = layoutRef.current
    const routedLeaf = route.terminalId && previousLayout
      ? leafOfSession(previousLayout, route.terminalId)
      : null
    const routedSession = route.terminalId
      ? nextSessions.find((item) => item.id === route.terminalId)
      : undefined
    const rememberedSession = nextSessions.find((item) => item.id === lastTerminalIdRef.current)
    const fallbackSession = routedSession || rememberedSession || nextSessions[0]

    // 布局只记录已打开到各 Pane 的 Tabs：轮询仅剔除已
    // 消失的 id，绝不把新发现的后台会话自动灌入 P1。
    const sessionIds = nextSessions.map((item) => item.id)
    let nextLayout = reconcile(previousLayout, sessionIds) ?? createLeaf()
    let nextFocusedLeafId = (
      (focusedLeafIdRef.current && findLeaf(nextLayout, focusedLeafIdRef.current))
      || firstLeaf(nextLayout)
    ).id
    let nextActiveId: string | null = null
    let nextMissingId: string | null = null

    if (route.page === 'terminals' && route.terminalId && routedSession) {
      // 已归属标签只 reveal 原 Pane；深链指向尚未归属的终端时，
      // 与设备组单击一致，作为当前 Pane 的临时 preview 打开。
      const activated = routedLeaf
        ? activateSession(nextLayout, routedSession.id)
        : previewSession(nextLayout, routedSession.id, { leafId: nextFocusedLeafId })
      nextLayout = activated.root
      nextFocusedLeafId = activated.leafId
      nextActiveId = routedSession.id
    } else if (route.page === 'terminals' && route.terminalId) {
      if (routedLeaf) {
        // 当前已打开的后台会话被外部关闭：在原 Pane 选中对账后的
        // 相邻 Tab，而不是把它误报为一个新的无效深链。
        const fallbackLeaf = findLeaf(nextLayout, routedLeaf.id)
          || (nextFocusedLeafId ? findLeaf(nextLayout, nextFocusedLeafId) : null)
          || firstLeaf(nextLayout)
        nextFocusedLeafId = fallbackLeaf.id
        nextActiveId = fallbackLeaf.activeTab
      } else {
        nextMissingId = route.terminalId
      }
    } else if (route.page === 'terminals') {
      const focusedLeaf = findLeaf(nextLayout, nextFocusedLeafId) || firstLeaf(nextLayout)
      nextFocusedLeafId = focusedLeaf.id
      nextActiveId = focusedLeaf.activeTab
    }

    setLayoutState(nextLayout)
    setFocusedLeafState(nextFocusedLeafId)
    activeIdRef.current = nextActiveId
    setActiveId(nextActiveId)
    setMissingTerminalId(nextMissingId)
    setWorkspaceSessionId((current) => current === null ? null : nextActiveId)

    if (nextActiveId) {
      lastTerminalIdRef.current = nextActiveId
      storeTerminalId(nextActiveId)
    } else if (!nextSessions.some((item) => item.id === lastTerminalIdRef.current)) {
      // 空 Pane 不清除最近记录，但已不存在的记录要换成一个有效 fallback。
      lastTerminalIdRef.current = fallbackSession?.id ?? null
      storeTerminalId(lastTerminalIdRef.current)
    }

    if (route.page === 'terminals' && !nextMissingId) {
      const path = nextActiveId ? `/terminal/${encodeURIComponent(nextActiveId)}` : '/terminals'
      if (window.location.pathname !== path) window.history.replaceState({}, '', path)
    }
  }, [])

  const load = useCallback(async () => {
    const requestId = ++loadRequestIdRef.current
    const mutationEpoch = sessionMutationEpochRef.current
    const [nextDevices, nextSessions, nextPreviews] = await Promise.all([api.devices(), api.terminals(), api.previews()])
    // A slower, older poll must never overwrite a newer response. Device and
    // preview snapshots are safe to apply while a terminal mutation is pending,
    // but the session/layout snapshot is not.
    if (requestId !== loadRequestIdRef.current) return
    const devicesSignature = JSON.stringify(nextDevices)
    if (devicesSignature !== devicesSignatureRef.current) {
      devicesSignatureRef.current = devicesSignature
      setDevices(nextDevices)
    }
    const previewsSignature = JSON.stringify(nextPreviews)
    if (previewsSignature !== previewsSignatureRef.current) {
      previewsSignatureRef.current = previewsSignature
      setPreviews(nextPreviews)
    }
    const currentPreview = activePreviewRef.current
    if (currentPreview && !nextPreviews.some((item) => item.id === currentPreview.preview.id)) {
      activePreviewRef.current = null
      setActivePreview(null)
      setError('开发服务已停止，关联预览隧道已自动关闭')
    }
    if (
      sessionMutationDepthRef.current > 0 ||
      mutationEpoch !== sessionMutationEpochRef.current
    ) return
    applySessions(nextSessions)
  }, [applySessions])

  useEffect(() => {
    activePreviewRef.current = activePreview
  }, [activePreview])

  useEffect(() => {
    let cancelled = false

    const start = async () => {
      try {
        const { build_sha: backendBuildSha } = await api.version()
        if (frontendBuildSha !== 'development' && frontendBuildSha !== backendBuildSha) {
          throw new Error(`前后端版本不一致（前端 ${frontendBuildSha.slice(0, 12)}，后端 ${backendBuildSha.slice(0, 12)}）`)
        }
      } catch (reason) {
        if (cancelled) return
        if (reason instanceof Error && reason.message.startsWith('前后端版本不一致')) {
          setStartupError(`${reason.message}。部署已被安全拦截，请刷新或回滚版本。`)
          return
        }
        setStartupError('无法验证前后端发布版本。部署已被安全拦截，请检查后端版本或回滚。')
        return
      }

      let name: string
      try {
        const identity = await api.me()
        name = identity.username
      } catch (reason) {
        if (cancelled) return
        if (reason instanceof Error && 'status' in reason && reason.status === 401) {
          setUsername(null)
          return
        }
        setStartupError('无法确认登录状态，请检查网络连接后刷新页面。')
        return
      }

      if (cancelled) return
      setUsername(name)
      try {
        await load()
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '无法加载控制台数据')
      }
    }

    void start()
    return () => { cancelled = true }
  }, [load])
  // 会话生命周期与服务状态由服务端推送。在 tmux 后端下,原来每 5 秒一次的
  // /api/terminals 轮询会让每个标签页都触发一轮 tmux 查询,而绝大多数时候
  // 什么都没变。
  useEffect(() => {
    if (!username) return
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let reconnectAttempt = 0
    let disposed = false

    const connect = () => {
      if (disposed) return
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/sessions`)
      socket.onopen = () => {
        reconnectAttempt = 0
        sessionsPushHealthyRef.current = true
      }
      socket.onmessage = (event) => {
        // 本地变更进行中时跳过:那次变更自己的 load() 会给出最终状态,
        // 与轮询路径的 mutation guard 保持一致。
        if (sessionMutationDepthRef.current > 0) return
        try {
          applySessions(normalizeTerminalSessions(JSON.parse(String(event.data))))
        } catch {
          // 畸形帧不该打断连接;下一次推送仍是完整快照。
        }
      }
      socket.onclose = (event) => {
        sessionsPushHealthyRef.current = false
        if (disposed || event.code === 4401) return
        const delay = Math.min(750 * 2 ** reconnectAttempt, 8000)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
      socket.onerror = () => socket?.close()
    }
    connect()

    return () => {
      disposed = true
      sessionsPushHealthyRef.current = false
      window.clearTimeout(reconnectTimer)
      socket?.close(1000)
    }
  }, [applySessions, username])

  // 轮询退化为兜底:devices/previews 没有推送通道,而推送断开时它还要
  // 顶上会话状态。隐藏的标签页不轮询,重新可见时立刻补一次。
  useEffect(() => {
    if (!username) return
    let timer: number | undefined
    let cancelled = false
    const schedule = () => {
      if (cancelled) return
      const interval = sessionsPushHealthyRef.current ? 30_000 : 5_000
      timer = window.setTimeout(async () => {
        if (document.visibilityState === 'visible') await load().catch(() => undefined)
        schedule()
      }, interval)
    }
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void load().catch(() => undefined)
    }
    schedule()
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [load, username])
  useEffect(() => {
    const media = window.matchMedia(SINGLE_PANE_MEDIA_QUERY)
    const onChange = () => setForceSingle(media.matches)
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])
  useEffect(() => {
    if (focusMode && (!layout || listLeaves(layout).length <= 1)) setFocusMode(false)
  }, [focusMode, layout])
  useEffect(() => {
    try {
      if (layout) window.localStorage.setItem(LAYOUT_KEY, serializeLayout(layout, focusedLeafId))
      else window.localStorage.removeItem(LAYOUT_KEY)
    } catch {
      // Layout persistence is best-effort.
    }
  }, [layout, focusedLeafId])
  useEffect(() => {
    const onPopState = () => {
      const route = routeFromLocation()
      let nextId: string | null = null
      let nextMissingId: string | null = null

      if (route.page === 'terminals' && route.terminalId) {
        const session = sessions.find((item) => item.id === route.terminalId)
        if (session) {
          const root = layoutRef.current ?? createLeaf()
          const currentLeaf = root ? leafOfSession(root, session.id) : null
          const activated = currentLeaf
            ? activateSession(root, session.id)
            : previewSession(root, session.id, { leafId: focusedLeafIdRef.current })
          setLayoutState(activated.root)
          setFocusedLeafState(activated.leafId)
          nextId = session.id
        } else {
          nextMissingId = route.terminalId
        }
      } else if (route.page === 'terminals') {
        const root = layoutRef.current
        if (root) {
          const leaf = (focusedLeafIdRef.current && findLeaf(root, focusedLeafIdRef.current))
            || firstLeaf(root)
          setFocusedLeafState(leaf.id)
          nextId = leaf.activeTab
        } else {
          const leaf = createLeaf()
          setLayoutState(leaf)
          setFocusedLeafState(leaf.id)
        }
      }

      // History navigation is an in-memory transition first. A failed refresh
      // must not leave URL/active/focused leaf split across three states.
      activeIdRef.current = nextId
      setActiveId(nextId)
      setWorkspaceSessionId((current) => current === null ? null : nextId)
      setMissingTerminalId(nextMissingId)
      setPage(route.page)
      if (nextId) {
        lastTerminalIdRef.current = nextId
        storeTerminalId(nextId)
        focusTerminalInput(nextId)
      }
      void load().catch(() => undefined)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [load, sessions])

  const showTerminal = (id: string | null, options: ShowTerminalOptions = {}) => {
    const disposition = options.disposition ?? 'preview'
    const replace = options.replace ?? disposition === 'preview'
    const { focusInput = true, preserveLast = false, targetLeafId } = options
    activeIdRef.current = id; setActiveId(id); setMissingTerminalId(null); setPage('terminals')
    setWorkspaceSessionId((current) => current === null ? null : id)
    if (id || !preserveLast) {
      lastTerminalIdRef.current = id
      storeTerminalId(id)
    }
    if (id) {
      const root = layoutRef.current ?? createLeaf()
      const requestedTarget = targetLeafId && findLeaf(root, targetLeafId)
        ? targetLeafId
        : (focusedLeafIdRef.current && findLeaf(root, focusedLeafIdRef.current)
            ? focusedLeafIdRef.current
            : firstLeaf(root).id)
      const activated = disposition === 'pinned'
        ? pinSession(root, id, { leafId: requestedTarget })
        : disposition === 'select'
          ? activateSession(root, id, { leafId: requestedTarget })
          : previewSession(root, id, { leafId: requestedTarget })
      setLayoutState(activated.root)
      setFocusedLeafState(activated.leafId)
    }
    const path = id ? `/terminal/${encodeURIComponent(id)}` : '/terminals'
    if (window.location.pathname !== path) window.history[replace ? 'replaceState' : 'pushState']({}, '', path)
    if (id && focusInput) focusTerminalInput(id)
  }
  const focusLeaf = (leafId: string) => {
    const leaf = layoutRef.current ? findLeaf(layoutRef.current, leafId) : null
    if (!leaf) return
    if (focusedLeafIdRef.current !== leafId) setFocusedLeafState(leafId)
    const id = leaf?.activeTab ?? null
    if (id === activeIdRef.current) return
    activeIdRef.current = id
    setActiveId(id)
    setMissingTerminalId(null)
    setWorkspaceSessionId((current) => current === null ? null : id)
    if (id) {
      lastTerminalIdRef.current = id
      storeTerminalId(id)
      const path = `/terminal/${encodeURIComponent(id)}`
      if (window.location.pathname !== path) window.history.replaceState({}, '', path)
    } else if (window.location.pathname !== '/terminals') {
      // 空 Pane 也是一个可聚焦的 editor group。它没有选中 Tab，
      // 因此全局 active 与 URL 都必须同步清空。
      window.history.replaceState({}, '', '/terminals')
    }
  }
  const activatePaneTab = (
    sessionId: string,
    leafId: string,
    options: { focusInput?: boolean } = {},
  ) => {
    const root = layoutRef.current
    const leaf = root ? findLeaf(root, leafId) : null
    // Pane-local 切换是严格 select，不具有 attach/move 副作用。
    if (!leaf?.tabs.includes(sessionId)) return
    showTerminal(sessionId, {
      disposition: 'select',
      targetLeafId: leafId,
      focusInput: options.focusInput,
      replace: true,
    })
  }
  const pinPaneTab = (sessionId: string, leafId: string) => {
    const leaf = layoutRef.current ? findLeaf(layoutRef.current, leafId) : null
    if (!leaf?.tabs.includes(sessionId)) return
    showTerminal(sessionId, {
      disposition: 'pinned',
      targetLeafId: leafId,
      replace: true,
    })
  }
  const splitTerminal = (leafId: string, direction: LayoutDirection) => {
    const currentLayout = layoutRef.current
    if (!currentLayout) return
    const result = splitLeaf(currentLayout, leafId, direction)
    if (!result) return
    setLayoutState(result.root)
    setFocusedLeafState(result.newLeafId)
    activeIdRef.current = null
    setActiveId(null)
    setMissingTerminalId(null)
    setWorkspaceSessionId(null)
    setPage('terminals')
    if (window.location.pathname !== '/terminals') {
      window.history.pushState({}, '', '/terminals')
    }
  }
  const closeTerminalPane = (leafId: string) => {
    const root = layoutRef.current
    if (!root) return
    const result = closeLeaf(root, leafId)
    if (!result) return
    setLayoutState(result.root)
    setFocusedLeafState(result.focusedLeafId)
    const nextLeaf = findLeaf(result.root, result.focusedLeafId)
    const nextId = nextLeaf?.activeTab ?? nextLeaf?.tabs[0] ?? null
    activeIdRef.current = nextId
    setActiveId(nextId)
    lastTerminalIdRef.current = nextId
    storeTerminalId(nextId)
    setWorkspaceSessionId((current) => current === null ? null : nextId)
    if (nextId) {
      const path = `/terminal/${encodeURIComponent(nextId)}`
      if (window.location.pathname !== path) window.history.replaceState({}, '', path)
      focusTerminalInput(nextId)
    } else if (window.location.pathname !== '/terminals') {
      window.history.replaceState({}, '', '/terminals')
    }
  }
  const detachPaneTab = (sessionId: string, leafId: string) => {
    const root = layoutRef.current
    if (!root) return
    const next = detachSession(root, leafId, sessionId)
    if (next === root) return
    setLayoutState(next)
    // 只有当前 active pane 的选择需要同步全局 active/URL。关闭其他
    // pane 的标签不会偷走焦点，也不会触碰后台终端。
    if (focusedLeafIdRef.current === leafId) focusLeaf(leafId)
  }
  const movePaneTab = (
    sessionId: string,
    sourceLeafId: string,
    targetLeafId: string,
    targetIndex: number,
  ) => {
    const root = layoutRef.current
    if (!root) return
    const next = moveSession(root, sessionId, {
      sourceLeafId,
      targetLeafId,
      targetIndex,
    })
    if (next === root) return
    const targetLeaf = findLeaf(next, targetLeafId)
    if (!targetLeaf?.tabs.includes(sessionId)) return

    setLayoutState(next)
    setFocusedLeafState(targetLeafId)
    activeIdRef.current = sessionId
    setActiveId(sessionId)
    setMissingTerminalId(null)
    setPage('terminals')
    setWorkspaceSessionId((current) => current === null ? null : sessionId)
    lastTerminalIdRef.current = sessionId
    storeTerminalId(sessionId)
    const path = `/terminal/${encodeURIComponent(sessionId)}`
    if (window.location.pathname !== path) window.history.replaceState({}, '', path)
    focusTerminalInput(sessionId)
  }
  const navigate = (nextPage: MainPage, replace = false) => {
    if (nextPage === 'terminals') {
      const root = layoutRef.current
      if (root) {
        const leaf = (focusedLeafIdRef.current && findLeaf(root, focusedLeafIdRef.current)) || firstLeaf(root)
        setFocusedLeafState(leaf.id)
        // 恢复持久化 workspace：聚焦 Pane 若为空，保持为空，不用
        // LAST_TERMINAL_KEY 把一个后台会话暗中填回。
        showTerminal(leaf.activeTab, { replace, preserveLast: true })
      } else {
        const leaf = createLeaf()
        setLayoutState(leaf)
        setFocusedLeafState(leaf.id)
        showTerminal(null, { replace, preserveLast: true })
      }
      return
    }
    activeIdRef.current = null; setActiveId(null); setMissingTerminalId(null); setPage(nextPage)
    setWorkspaceSessionId(null)
    const path = nextPage === 'setup' ? '/setup' : '/'
    if (window.location.pathname !== path) window.history[replace ? 'replaceState' : 'pushState']({}, '', path)
  }
  const openTerminal = async (device: Device, targetLeafId?: string | null) => {
    const capturedTargetLeafId = targetLeafId ?? focusedLeafIdRef.current
    beginSessionMutation()
    setBusyId(device.id); setError('')
    try {
      const session = await api.createDeviceTerminal(device.id, device.name)
      setSessions((current) => current.some((item) => item.id === session.id) ? current : [...current, session])
      showTerminal(session.id, { disposition: 'pinned', targetLeafId: capturedTargetLeafId })
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法打开终端') }
    finally {
      finishSessionMutation()
      setBusyId(null)
      void load().catch(() => undefined)
    }
  }
  const cloneTerminal = async (source: TerminalSession, targetLeafId?: string | null) => {
    if (cloningRef.current) return
    const capturedTargetLeafId = targetLeafId ?? focusedLeafIdRef.current
    cloningRef.current = true
    beginSessionMutation()
    setCloningId(source.id); setError('')
    try {
      const session = source.device_id
        ? await api.createDeviceTerminal(
            source.device_id,
            source.device_name || source.name,
            source.workspace?.root,
          )
        : await api.createTerminal(source.name, source.workspace?.root)
      setSessions((current) => current.some((item) => item.id === session.id) ? current : [...current, session])
      showTerminal(session.id, { disposition: 'pinned', targetLeafId: capturedTargetLeafId })
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法克隆终端') }
    finally {
      cloningRef.current = false
      finishSessionMutation()
      setCloningId(null)
      void load().catch(() => undefined)
    }
  }
  const closeTerminal = async () => {
    const session = closeTarget
    if (!session) return
    setCloseTarget(null)
    beginSessionMutation()
    try {
      await api.deleteTerminal(session.id)
      setSessions((current) => current.filter((item) => item.id !== session.id))
      if (workspaceSessionId === session.id) setWorkspaceSessionId(null)
      // 设备组中的关闭是唯一会终止后台的入口。成功后再从所有
      // pane 解除该终端的显示归属。
      const nextLayout = layoutRef.current ? removeSession(layoutRef.current, session.id) : null
      setLayoutState(nextLayout)
      if (activeIdRef.current === session.id) {
        if (nextLayout && focusedLeafIdRef.current && findLeaf(nextLayout, focusedLeafIdRef.current)) {
          focusLeaf(focusedLeafIdRef.current)
        } else {
          showTerminal(null, { replace: true, preserveLast: true })
        }
      } else if (nextLayout && focusedLeafIdRef.current && !findLeaf(nextLayout, focusedLeafIdRef.current)) {
        setFocusedLeafState(firstLeaf(nextLayout).id)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法关闭终端')
    } finally {
      finishSessionMutation()
      void load().catch(() => undefined)
    }
  }
  const sync = async () => { setError(''); try { await api.syncDevices(); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '同步失败') } }
  const probe = async (device: Device) => { setBusyId(device.id); try { await api.probeDevice(device.id); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '检测失败') } finally { setBusyId(null) } }
  const remove = async (device: Device) => { if (!window.confirm(`删除设备 ${device.name}？`)) return; try { await api.deleteDevice(device.id); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败') } }
  const logout = async () => { await api.logout(); invalidateSnapshots(); setUsername(null); setRuntimeDevice(null); setDevices([]); setSessions([]); setPreviews([]); setWorkspaceSessionId(null); activePreviewRef.current = null; setActivePreview(null) }
  const stopPreview = async (preview: Preview) => {
    try {
      await api.deletePreview(preview.id)
      invalidateSnapshots()
      setPreviews((current) => current.filter((item) => item.id !== preview.id))
      if (activePreview?.preview.id === preview.id) { activePreviewRef.current = null; setActivePreview(null) }
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法关闭预览') }
  }
  const openDetectedService = async (session: TerminalSession, service: DetectedService) => {
    if (!session.device_id || service.status !== 'online') return
    setPreviewBusy({ terminalId: session.id, port: service.port })
    setError('')
    try {
      const preview = await api.createPreview(
        session.device_id,
        service.port,
        service.label,
        session.id,
      )
      const { url } = await api.previewTicket(preview.id)
      invalidateSnapshots()
      setPreviews((current) => [preview, ...current.filter((item) => item.id !== preview.id)])
      activePreviewRef.current = { preview, url }
      setActivePreview({ preview, url })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法打开检测到的开发服务')
      await load().catch(() => undefined)
    } finally {
      setPreviewBusy(null)
    }
  }

  // Agent 工作目录跟随:当前聚焦终端的 agent cwd 落在其 workspace.root 之内
  // 且相对路径非空时,提示是否跳转文件树。windows 设备路径处理不在本期范围。
  useEffect(() => {
    const agentWorkingDirectory = (session: TerminalSession) => {
      const projected = activeAgentForTerminal(execution.snapshot, session.id)
      if (projected) {
        const cwdEvidence = fieldEvidence(projected, 'cwd')
        if (
          projected.kind
          && projected.cwd
          && cwdEvidence
          && evidenceFreshness(cwdEvidence, execution.freshness_now) === 'fresh'
        ) {
          return { kind: projected.kind, cwd: projected.cwd }
        }
        // cwd is not yet part of every projection shape. Preserve the legacy
        // terminal-session compatibility view until its field-level evidence
        // is available, without borrowing lifecycle evidence as cwd evidence.
        return session.agent?.cwd
          ? { kind: session.agent.kind, cwd: session.agent.cwd }
          : null
      }
      return session.agent?.cwd
        ? { kind: session.agent.kind, cwd: session.agent.cwd }
        : null
    }
    if (agentSwitchTarget) {
      // 弹窗期间 agent 退出/换目录或会话被关闭时,自动收起弹窗。
      const session = sessions.find((item) => item.id === agentSwitchTarget.sessionId)
      if (!session || agentWorkingDirectory(session)?.cwd !== agentSwitchTarget.cwd) setAgentSwitchTarget(null)
      return
    }
    const session = sessions.find((item) => item.id === activeId)
    if (!session?.active) return
    const agent = agentWorkingDirectory(session)
    const workspace = session.workspace
    if (!agent?.cwd || !workspace?.root || workspace.platform !== 'posix') return
    const root = workspace.root.replace(/\/+$/, '')
    const cwd = agent.cwd
    if (cwd !== root && !cwd.startsWith(`${root}/`)) return
    const relPath = cwd === root ? '' : cwd.slice(root.length + 1)
    if (!relPath) return
    if (agentPromptedRef.current.get(session.id) === cwd) return
    agentPromptedRef.current.set(session.id, cwd)
    setAgentSwitchTarget({ sessionId: session.id, kind: agent.kind, cwd, relPath })
  }, [sessions, activeId, agentSwitchTarget, execution.freshness_now, execution.snapshot])

  const jumpToAgentDirectory = () => {
    const target = agentSwitchTarget
    if (!target) return
    setAgentSwitchTarget(null)
    if (workspaceSessionId !== target.sessionId) setWorkspaceSessionId(target.sessionId)
    setWorkspaceFocusPath(target.relPath)
  }

  if (startupError) {
    return (
      <div className="grid h-full w-full place-items-center bg-background p-6 text-center text-sm text-[#ffadb5]">
        <div className="max-w-xl rounded-xl border border-[#713640] bg-[#28171bdd] p-5">{startupError}</div>
      </div>
    )
  }
  if (username === undefined) {
    return (
      <div className="grid h-full w-full place-items-center bg-background">
        <div className="h-6 w-3 animate-blink bg-primary" />
      </div>
    )
  }
  if (username === null) return <Login onLogin={(name) => { setUsername(name); void load() }} />
  const activeSession = sessions.find((item) => item.id === activeId)
  const workspaceSession = sessions.find((item) => item.id === workspaceSessionId)
  const showTerminalTabs = page === 'terminals' && sessions.length > 0
  return (
    <ExecutionProvider value={execution}>
    <main className="grid h-full w-full grid-rows-[58px_minmax(0,1fr)] bg-background">
      <Topbar
        username={username}
        page={page}
        deviceSummary={`${devices.filter((device) => device.frp_online).length}/${devices.length}`}
        sessionCount={sessions.length}
        onNavigate={navigate}
        onShowPassword={() => setShowPassword(true)}
        onLogout={() => void logout()}
      />
      <section className={cn('grid min-h-0 min-w-0', showTerminalTabs ? 'grid-rows-[44px_minmax(0,1fr)] max-md:grid-rows-[auto_minmax(0,1fr)]' : 'grid-rows-[minmax(0,1fr)]')}>
        {showTerminalTabs && (
          <TerminalTabsBar
            devices={devices}
            sessions={sessions}
            activeId={activeId}
            cloningId={cloningId}
            layout={layout}
            focusedLeafId={focusedLeafId}
            focusMode={focusMode}
            coarseMode={forceSingle}
            onToggleFocusMode={() => {
              setFocusMode((current) => !current)
              if (activeId) focusTerminalInput(activeId)
            }}
            onPreviewTerminal={(id) => showTerminal(id, { disposition: 'preview' })}
            onPinTerminal={(id) => showTerminal(id, { disposition: 'pinned' })}
            onRequestTerminate={setCloseTarget}
            onClone={(source) => void cloneTerminal(source)}
            onPreviewServices={(source) => setPreviewTarget({ deviceId: source.device_id || undefined, terminalId: source.id })}
          />
        )}
        <div className="relative min-h-0 min-w-0 overflow-auto">
          {layout && (
            <div className={cn(
              'absolute inset-3 overflow-hidden rounded-[10px] border border-[#222d37] bg-[#0b0f14] shadow-[0_20px_55px_#0006] max-md:inset-1.5',
              !(page === 'terminals' && !missingTerminalId) && 'invisible pointer-events-none',
            )}>
              <div className="flex h-full min-h-0 w-full min-w-0">
                <div className={cn('min-h-0 min-w-0 flex-1', page === 'terminals' && activeSession && workspaceSession?.id === activeSession.id && 'max-md:hidden')}>
                  <TerminalGrid
                    layout={layout}
                    devices={devices}
                    sessions={sessions}
                    pageVisible={page === 'terminals' && !missingTerminalId}
                    activeId={activeId}
                    focusedLeafId={focusedLeafId}
                    forceSingle={forceSingle || focusMode}
                    previewBusy={previewBusy}
                    creatingTab={cloningId !== null}
                    onFocusLeaf={focusLeaf}
                    onActivateTab={(sessionId, leafId, options) => {
                      activatePaneTab(sessionId, leafId, { focusInput: options.focusTerminal })
                    }}
                    onPinTab={pinPaneTab}
                    onDetachTab={detachPaneTab}
                    onMoveTab={movePaneTab}
                    onNewTab={(source, leafId) => void cloneTerminal(source, leafId)}
                    onRatio={(splitId, ratio) => {
                      if (layoutRef.current) setLayoutState(setRatio(layoutRef.current, splitId, ratio))
                    }}
                    onSplit={splitTerminal}
                    onClosePane={closeTerminalPane}
                    onPreviewService={(session, service) => void openDetectedService(session, service)}
                    onOpenWorkspace={(session) => setWorkspaceSessionId(session.id)}
                  />
                </div>
                {page === 'terminals' && activeSession && workspaceSession?.id === activeSession.id && (
                  <WorkspacePane
                    key={workspaceSession.id}
                    session={workspaceSession}
                    focusPath={workspaceFocusPath}
                    onFocusPathConsumed={() => setWorkspaceFocusPath(null)}
                    onClose={() => setWorkspaceSessionId(null)}
                  />
                )}
              </div>
            </div>
          )}
          {page === 'terminals' && activeSession && (
            <TerminalRoomOverview
              devices={devices}
              sessions={sessions}
              busyId={busyId}
              activeSessionId={activeId}
              onOpen={(device) => void openTerminal(device)}
              onProbe={(device) => void probe(device)}
              onEdit={setDeviceDialog}
              onSelectTerminal={(sessionId) => showTerminal(sessionId)}
            />
          )}
          {page === 'terminals' ? (
            (!layout || Boolean(missingTerminalId)) && (
              <TerminalEmpty
                missingTerminalId={missingTerminalId}
                hasSessions={sessions.length > 0}
                onBack={() => navigate('devices')}
              />
            )
          ) : page === 'setup' ? (
            <DownloadsPage devices={devices} onChanged={() => void load()} />
          ) : (
            <DeviceDashboard
              devices={devices}
              sessions={sessions}
              busyId={busyId}
              onOpen={(device) => void openTerminal(device)}
              onEdit={setDeviceDialog}
              onDelete={(device) => void remove(device)}
              onProbe={(device) => void probe(device)}
              onSync={() => void sync()}
              onAdd={() => setDeviceDialog('new')}
              onSelectTerminal={(sessionId) => showTerminal(sessionId)}
              onPreview={(device) => setPreviewTarget({ deviceId: device?.id })}
              onRuntime={setRuntimeDevice}
            />
          )}
        </div>
      </section>
      {error && (
        <button onClick={() => setError('')} className="fixed right-5 bottom-5 flex cursor-pointer items-center gap-4 rounded-lg border border-[#713640] bg-[#28171bdd] px-3.5 py-[11px] text-xs text-[#ffadb5] shadow-[0_14px_40px_#0008] max-md:right-3 max-md:bottom-3 max-md:left-3 max-md:justify-between">
          {error}
          <XIcon className="size-3.5 flex-none" />
        </button>
      )}
      {showPassword && <PasswordDialog onClose={() => setShowPassword(false)} />}
      {closeTarget && (
        <Dialog open onOpenChange={(open) => { if (!open) setCloseTarget(null) }}>
          <DialogContent className="sm:max-w-[410px]">
            <DialogHeader>
              <Eyebrow>TERMINAL</Eyebrow>
              <DialogTitle>终止后台终端</DialogTitle>
              <DialogDescription>
                终止 {closeTarget.name} ({closeTarget.id.slice(0, 8)})？后台会话将被结束，未保存的输出会丢失。
              </DialogDescription>
            </DialogHeader>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setCloseTarget(null)}>取消</Button>
              <Button variant="destructive" onClick={() => void closeTerminal()}>终止后台终端</Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
      {agentSwitchTarget && (
        <Dialog open onOpenChange={(open) => { if (!open) setAgentSwitchTarget(null) }}>
          <DialogContent className="sm:max-w-[410px]">
            <DialogHeader>
              <Eyebrow>AGENT</Eyebrow>
              <DialogTitle>{agentDisplayName(agentSwitchTarget.kind)} 正在其他目录工作</DialogTitle>
              <DialogDescription>
                {agentDisplayName(agentSwitchTarget.kind)} 正在 {agentSwitchTarget.cwd} 工作
                （工作区内路径 {agentSwitchTarget.relPath}），是否跳转文件树？
              </DialogDescription>
            </DialogHeader>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setAgentSwitchTarget(null)}>忽略</Button>
              <Button onClick={jumpToAgentDirectory}>跳转</Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
      {deviceDialog && (
        <DeviceDialog
          device={deviceDialog === 'new' ? undefined : deviceDialog}
          onClose={() => setDeviceDialog(null)}
          onSaved={() => void load()}
        />
      )}
      {runtimeDevice && (
        <DeviceRuntimeDialog
          key={runtimeDevice.id}
          device={devices.find((device) => device.id === runtimeDevice.id) || runtimeDevice}
          onClose={() => setRuntimeDevice(null)}
          onChanged={() => void load()}
        />
      )}
      {previewTarget && (
        <PreviewDialog
          devices={devices}
          sessions={sessions}
          previews={previews}
          initialDeviceId={previewTarget.deviceId}
          initialTerminalId={previewTarget.terminalId}
          onClose={() => setPreviewTarget(null)}
          onCreated={(preview, url) => {
            invalidateSnapshots()
            setPreviews((current) => [preview, ...current.filter((item) => item.id !== preview.id)])
            activePreviewRef.current = { preview, url }
            setActivePreview({ preview, url })
            setPreviewTarget(null)
          }}
          onDeleted={(id) => {
            invalidateSnapshots()
            setPreviews((current) => current.filter((item) => item.id !== id))
            if (activePreview?.preview.id === id) { activePreviewRef.current = null; setActivePreview(null) }
          }}
        />
      )}
      {activePreview && (
        <PreviewPane
          preview={activePreview.preview}
          authorizedUrl={activePreview.url}
          topOffset={58 + (showTerminalTabs ? 44 : 0)}
          onClose={() => setActivePreview(null)}
          onStop={() => void stopPreview(activePreview.preview)}
        />
      )}
      <ExecutionStatusNotice />
    </main>
    </ExecutionProvider>
  )
}
