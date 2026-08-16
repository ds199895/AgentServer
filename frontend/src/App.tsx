import { useCallback, useEffect, useRef, useState } from 'react'
import { XIcon } from 'lucide-react'

import { api, frontendBuildSha, type DetectedService, type Device, type Preview, type TerminalSession } from '@/api'
import { DeviceDashboard } from '@/components/DeviceDashboard'
import { DeviceDialog } from '@/components/DeviceDialog'
import { DownloadsPage } from '@/components/DownloadsPage'
import { Eyebrow } from '@/components/Eyebrow'
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
  findLeaf,
  firstLeaf,
  leafOfSession,
  listLeaves,
  parseLayout,
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
import { cn } from '@/lib/utils'

const LAST_TERMINAL_KEY = 'agentserver:last-terminal-id'
const LAYOUT_KEY = 'agentserver:terminal-layout-v1'

type ShowTerminalOptions = {
  replace?: boolean
  /** 键盘在 TabStrip 中导航时保留 Tab 焦点，不抢回 xterm。 */
  focusInput?: boolean
  /** 聚焦空 Pane 时保留“最近终端”仅供用户后续显式 reveal。 */
  preserveLast?: boolean
  /** 新会话应进入的明确窗格；已有会话仍按 reveal 规则留在原窗格。 */
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
  return window.matchMedia('(max-width: 767px), (pointer: coarse)').matches
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
    const pane = [...document.querySelectorAll<HTMLElement>('[data-terminal-id]')]
      .find((element) => element.dataset.terminalId === id)
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
  const [previewTarget, setPreviewTarget] = useState<{ deviceId?: string; terminalId?: string } | null>(null)
  const [activePreview, setActivePreview] = useState<{ preview: Preview; url: string } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [cloningId, setCloningId] = useState<string | null>(null)
  const [closeTarget, setCloseTarget] = useState<TerminalSession | null>(null)
  const [previewBusy, setPreviewBusy] = useState<{ terminalId: string; port: number } | null>(null)
  const [workspaceSessionId, setWorkspaceSessionId] = useState<string | null>(null)
  const [layout, setLayout] = useState<LayoutNode | null>(() => initialLayout().layout)
  const [focusedLeafId, setFocusedLeafId] = useState<string | null>(() => initialLayout().focusedLeafId)
  const [forceSingle, setForceSingle] = useState(isCoarseLayout)
  const [focusMode, setFocusMode] = useState(false)
  const [splitting, setSplitting] = useState(false)
  const [findTabRequest, setFindTabRequest] = useState<{
    requestId: number
    targetLeafId: string
  } | null>(null)
  const [error, setError] = useState('')
  const [startupError, setStartupError] = useState('')
  const activeIdRef = useRef<string | null>(routeFromLocation().terminalId)
  const lastTerminalIdRef = useRef<string | null>(routeFromLocation().terminalId || storedTerminalId())
  const activePreviewRef = useRef<{ preview: Preview; url: string } | null>(null)
  const layoutRef = useRef<LayoutNode | null>(layout)
  const focusedLeafIdRef = useRef<string | null>(focusedLeafId)
  const splittingRef = useRef(false)
  const cloningRef = useRef(false)
  const loadRequestIdRef = useRef(0)
  const sessionMutationEpochRef = useRef(0)
  const sessionMutationDepthRef = useRef(0)
  const findTabRequestIdRef = useRef(0)

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

  const beginSessionMutation = () => {
    sessionMutationDepthRef.current += 1
    sessionMutationEpochRef.current += 1
  }
  const finishSessionMutation = () => {
    sessionMutationDepthRef.current = Math.max(0, sessionMutationDepthRef.current - 1)
    sessionMutationEpochRef.current += 1
  }

  const load = useCallback(async () => {
    const requestId = ++loadRequestIdRef.current
    const mutationEpoch = sessionMutationEpochRef.current
    const [nextDevices, nextSessions, nextPreviews] = await Promise.all([api.devices(), api.terminals(), api.previews()])
    // A slower, older poll must never overwrite a newer response. Device and
    // preview snapshots are safe to apply while a terminal mutation is pending,
    // but the session/layout snapshot is not.
    if (requestId !== loadRequestIdRef.current) return
    setDevices(nextDevices)
    setPreviews(nextPreviews)
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
    setSessions(nextSessions)
    const route = routeFromLocation()
    const previousLayout = layoutRef.current
    const routedLeaf = route.terminalId && previousLayout
      ? leafOfSession(previousLayout, route.terminalId)
      : null
    const routedSession = route.terminalId
      ? nextSessions.find((item) => item.id === route.terminalId)
      : undefined
    const rememberedSession = nextSessions.find((item) => item.id === lastTerminalIdRef.current)
    const preferredSession = routedSession || rememberedSession || nextSessions[0]

    // 布局只记录已打开到各 Pane 的 Tabs：轮询仅剔除已
    // 消失的 id，绝不把新发现的后台会话自动灌入 P1。
    const sessionIds = nextSessions.map((item) => item.id)
    let nextLayout = reconcile(previousLayout, sessionIds)
    let nextFocusedLeafId = nextLayout
      ? ((focusedLeafIdRef.current && findLeaf(nextLayout, focusedLeafIdRef.current)) || firstLeaf(nextLayout)).id
      : null
    let nextActiveId: string | null = null
    let nextMissingId: string | null = null

    if (route.page === 'terminals' && route.terminalId && routedSession) {
      // 深链、设备页、设备世界和全局搜索统一为 reveal：已归属
      // 的 Tab 只在原 Pane 激活；未归属的会话才进入当前 Pane。
      const activated = activateSession(nextLayout, routedSession.id, { leafId: nextFocusedLeafId })
      nextLayout = activated.root
      nextFocusedLeafId = activated.leafId
      nextActiveId = routedSession.id
    } else if (route.page === 'terminals' && route.terminalId) {
      if (routedLeaf && nextLayout) {
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
      if (nextLayout) {
        const focusedLeaf = (nextFocusedLeafId && findLeaf(nextLayout, nextFocusedLeafId)) || firstLeaf(nextLayout)
        nextFocusedLeafId = focusedLeaf.id
        nextActiveId = focusedLeaf.activeTab
      } else if (preferredSession) {
        // 仅在完全没有 workspace 布局时创建 P1。已持久化的空 Pane
        // 是用户显式关闭最后一个 Tab 的结果，不会被自动填充。
        const activated = activateSession(null, preferredSession.id)
        nextLayout = activated.root
        nextFocusedLeafId = activated.leafId
        nextActiveId = preferredSession.id
      }
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
      lastTerminalIdRef.current = preferredSession?.id ?? null
      storeTerminalId(lastTerminalIdRef.current)
    }

    if (route.page === 'terminals' && !nextMissingId) {
      const path = nextActiveId ? `/terminal/${encodeURIComponent(nextActiveId)}` : '/terminals'
      if (window.location.pathname !== path) window.history.replaceState({}, '', path)
    }
  }, [])

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
  useEffect(() => {
    if (!username) return
    const timer = window.setInterval(() => void load().catch(() => undefined), 5000)
    return () => window.clearInterval(timer)
  }, [load, username])
  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px), (pointer: coarse)')
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
          const root = layoutRef.current
          const currentLeaf = root ? leafOfSession(root, session.id) : null
          const activated = currentLeaf
            ? activateSession(root, session.id)
            : activateSession(root, session.id, { leafId: focusedLeafIdRef.current })
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
          const preferredSession = sessions.find((item) => item.id === lastTerminalIdRef.current)
            || sessions[0]
          if (preferredSession) {
            const activated = activateSession(null, preferredSession.id)
            setLayoutState(activated.root)
            setFocusedLeafState(activated.leafId)
            nextId = preferredSession.id
          }
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
    const { replace = false, focusInput = true, preserveLast = false, targetLeafId } = options
    activeIdRef.current = id; setActiveId(id); setMissingTerminalId(null); setPage('terminals')
    setWorkspaceSessionId((current) => current === null ? null : id)
    if (id || !preserveLast) {
      lastTerminalIdRef.current = id
      storeTerminalId(id)
    }
    if (id) {
      // 所有入口都遵循同一条 reveal 语义：已归属某个窗格的会话
      // 只在原窗格激活并聚焦，不再因“从哪里点击”而暗中跨窗格搬移。
      // 只有尚未进入布局的新会话才放入当前聚焦窗格。
      const root = layoutRef.current
      const currentLeaf = root ? leafOfSession(root, id) : null
      const requestedTarget = root && targetLeafId && findLeaf(root, targetLeafId)
        ? targetLeafId
        : focusedLeafIdRef.current
      const activated = currentLeaf
        ? activateSession(root, id)
        : activateSession(root, id, { leafId: requestedTarget })
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
      focusInput: options.focusInput,
      replace: options.focusInput === false,
    })
  }
  const splitTerminal = async (source: TerminalSession, leafId: string, direction: LayoutDirection) => {
    if (splittingRef.current) return
    splittingRef.current = true
    beginSessionMutation()
    setSplitting(true); setError('')
    try {
      // VSCode 式拆分:原终端留在原窗格,在同一设备(或本地)开一个新终端放进新窗格
      const session = source.device_id
        ? await api.createDeviceTerminal(source.device_id, source.device_name || source.name, source.workspace?.root)
        : await api.createTerminal(source.name, source.workspace?.root)
      setSessions((current) => current.some((item) => item.id === session.id)
        ? current.map((item) => item.id === session.id ? session : item)
        : [...current, session])
      // mutation epoch 会阻止 POST 期间的轮询改写 layout，因此可直接
      // 对用户点击时的 leaf 分屏，不做 remove + reinsert 的非原子组合。
      const currentLayout = layoutRef.current
      if (currentLayout && findLeaf(currentLayout, leafId)) {
        const result = splitLeaf(currentLayout, leafId, direction, session.id)
        if (result) {
          setLayoutState(result.root)
          setFocusedLeafState(result.newLeafId)
        } else {
          const activated = activateSession(currentLayout, session.id, { leafId })
          setLayoutState(activated.root)
          setFocusedLeafState(activated.leafId)
        }
      } else {
        const activated = activateSession(currentLayout, session.id, { leafId: focusedLeafIdRef.current })
        setLayoutState(activated.root)
        setFocusedLeafState(activated.leafId)
      }
      activeIdRef.current = session.id
      setActiveId(session.id)
      setMissingTerminalId(null)
      setPage('terminals')
      lastTerminalIdRef.current = session.id
      storeTerminalId(session.id)
      const path = `/terminal/${encodeURIComponent(session.id)}`
      if (window.location.pathname !== path) window.history.pushState({}, '', path)
      focusTerminalInput(session.id)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法拆分终端') }
    finally {
      splittingRef.current = false
      finishSessionMutation()
      setSplitting(false)
      void load().catch(() => undefined)
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
        const preferredSession = sessions.find((item) => item.id === lastTerminalIdRef.current) || sessions[0]
        showTerminal(preferredSession?.id ?? null, { replace })
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
      showTerminal(session.id, { targetLeafId: capturedTargetLeafId })
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
      showTerminal(session.id, { targetLeafId: capturedTargetLeafId })
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
      // 从布局树移除：同 leaf 的相邻标签自动递补；最后一个
      // Tab 关闭后保留空 Pane，只有显式“关闭窗格”才收缩分屏。
      const nextLayout = layoutRef.current ? removeSession(layoutRef.current, session.id) : null
      setLayoutState(nextLayout)
      if (activeIdRef.current === session.id) {
        const nextLeaf = nextLayout
          ? (focusedLeafIdRef.current && findLeaf(nextLayout, focusedLeafIdRef.current)) || firstLeaf(nextLayout)
          : null
        showTerminal(nextLeaf?.activeTab ?? nextLeaf?.tabs[0] ?? null, { replace: true })
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
  const logout = async () => { await api.logout(); setUsername(null); setDevices([]); setSessions([]); setPreviews([]); setWorkspaceSessionId(null); activePreviewRef.current = null; setActivePreview(null) }
  const stopPreview = async (preview: Preview) => {
    try {
      await api.deletePreview(preview.id)
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
            findTabRequest={findTabRequest}
            onFindTabRequestHandled={(requestId) => {
              setFindTabRequest((current) => current?.requestId === requestId ? null : current)
            }}
            onToggleFocusMode={() => {
              setFocusMode((current) => !current)
              if (activeId) focusTerminalInput(activeId)
            }}
            onSelect={(id, targetLeafId) => showTerminal(id, { targetLeafId })}
            onClone={(source) => void cloneTerminal(source)}
            onPreview={(source) => setPreviewTarget({ deviceId: source.device_id || undefined, terminalId: source.id })}
          />
        )}
        <div className="relative min-h-0 min-w-0 overflow-auto">
          {layout && (
            <div className={cn(
              'absolute inset-3 overflow-hidden rounded-[10px] border border-[#222d37] bg-[#0b0f14] shadow-[0_20px_55px_#0006] max-md:inset-1.5',
              !(page === 'terminals' && !missingTerminalId) && 'invisible pointer-events-none',
            )}>
              <TerminalGrid
                layout={layout}
                devices={devices}
                sessions={sessions}
                pageVisible={page === 'terminals' && !missingTerminalId}
                activeId={activeId}
                focusedLeafId={focusedLeafId}
                forceSingle={forceSingle || focusMode}
                previewBusy={previewBusy}
                splitting={splitting}
                creatingTab={cloningId !== null}
                onFocusLeaf={focusLeaf}
                onActivateTab={(sessionId, leafId, options) => {
                  activatePaneTab(sessionId, leafId, { focusInput: options.focusTerminal })
                }}
                onNewTab={(source, leafId) => void cloneTerminal(source, leafId)}
                onFindTab={(leafId) => {
                  if (sessions.length === 0) {
                    setError('当前没有可添加的后台终端，请先在设备页新建。')
                    navigate('devices')
                    return
                  }
                  findTabRequestIdRef.current += 1
                  setFindTabRequest({ requestId: findTabRequestIdRef.current, targetLeafId: leafId })
                }}
                onCloseTerminal={setCloseTarget}
                onRatio={(splitId, ratio) => {
                  if (layoutRef.current) setLayoutState(setRatio(layoutRef.current, splitId, ratio))
                }}
                onSplit={(session, leafId, direction) => void splitTerminal(session, leafId, direction)}
                onClosePane={closeTerminalPane}
                onPreviewService={(session, service) => void openDetectedService(session, service)}
                onOpenWorkspace={(session) => setWorkspaceSessionId(session.id)}
              />
              {page === 'terminals' && activeSession && workspaceSession?.id === activeSession.id && (
                <WorkspacePane key={workspaceSession.id} session={workspaceSession} onClose={() => setWorkspaceSessionId(null)} />
              )}
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
            <DownloadsPage devices={devices} />
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
              onAdd={() => navigate('setup')}
              onSelectTerminal={(sessionId) => showTerminal(sessionId)}
              onPreview={(device) => setPreviewTarget({ deviceId: device?.id })}
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
              <DialogTitle>关闭终端</DialogTitle>
              <DialogDescription>
                关闭 {closeTarget.name} ({closeTarget.id.slice(0, 8)})？后台会话将被终止，未保存的输出会丢失。
              </DialogDescription>
            </DialogHeader>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setCloseTarget(null)}>取消</Button>
              <Button variant="destructive" onClick={() => void closeTerminal()}>关闭终端</Button>
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
      {previewTarget && (
        <PreviewDialog
          devices={devices}
          sessions={sessions}
          previews={previews}
          initialDeviceId={previewTarget.deviceId}
          initialTerminalId={previewTarget.terminalId}
          onClose={() => setPreviewTarget(null)}
          onCreated={(preview, url) => {
            setPreviews((current) => [preview, ...current.filter((item) => item.id !== preview.id)])
            activePreviewRef.current = { preview, url }
            setActivePreview({ preview, url })
            setPreviewTarget(null)
          }}
          onDeleted={(id) => {
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
    </main>
  )
}
