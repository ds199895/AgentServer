import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  File,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  LoaderCircle,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  Radio,
  XIcon,
} from 'lucide-react'

import {
  api,
  type ArtifactAttachment,
  type ArtifactEvent,
  type FileGrant,
  type TerminalSession,
  type WorkspaceEntry,
} from '@/api'
import { ArtifactPreview } from '@/components/ArtifactPreview'
import { parentWorkspacePath, useWorkspaceTree } from '@/components/useWorkspaceTree'
import { cn } from '@/lib/utils'

type Props = {
  session: TerminalSession
  onClose: () => void
  /** 相对工作区根的路径;传入后文件树会展开祖先目录并选中该路径。 */
  focusPath?: string | null
  /** focusPath 应用完毕后回调,外部应据此清空 focusPath。 */
  onFocusPathConsumed?: () => void
}

type PaneTab = 'files' | 'artifacts'
type EventConnection = 'connecting' | 'online' | 'offline'

function isDirectory(entry: WorkspaceEntry): boolean {
  return entry.kind === 'directory'
}

function formatBytes(value?: number | null): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return ''
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(value >= 10 * 1024 ? 0 : 1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(value >= 10 * 1024 ** 2 ? 0 : 1)} MB`
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}

function entryIcon(entry: WorkspaceEntry) {
  if (isDirectory(entry)) return Folder
  const name = entry.name.toLowerCase()
  if (/\.(?:png|jpe?g|gif|webp|bmp|avif)$/.test(name)) return FileImage
  if (/\.(?:md|txt|json|ya?ml|toml|tsx?|jsx?|py|go|rs|java|c|cpp|h|css|scss|sh|sql|log)$/.test(name)) return FileText
  return File
}

function artifactFromUnknown(value: unknown): ArtifactEvent | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const nested = record.artifact && typeof record.artifact === 'object'
    ? record.artifact as Record<string, unknown>
    : record.data && typeof record.data === 'object'
      ? record.data as Record<string, unknown>
      : record
  const nestedFile = nested.file && typeof nested.file === 'object'
    ? nested.file as Record<string, unknown>
    : null
  const field = (key: string): unknown => nested[key] ?? nestedFile?.[key] ?? record[key]
  const path = field('path')
  if (typeof path !== 'string' || !path) return null
  const stringValue = (key: string): string | undefined => typeof field(key) === 'string' ? field(key) as string : undefined
  const numberValue = (key: string): number | undefined => typeof field(key) === 'number' ? field(key) as number : undefined
  const type = stringValue('type') || 'artifact'
  const createdAt = numberValue('created_at') || numberValue('timestamp') || Date.now() / 1000
  const attachmentValue = nested.attachment ?? record.attachment ?? nestedFile?.attachment
  const attachment = attachmentFromUnknown(attachmentValue)
  return {
    sequence: numberValue('sequence'),
    id: stringValue('id') || `${path}:${stringValue('version') || createdAt}`,
    type,
    event: stringValue('event') || type,
    owner: stringValue('owner'),
    terminal_id: stringValue('terminal_id'),
    name: stringValue('name') || path.split(/[\\/]/).filter(Boolean).pop() || path,
    path,
    media_type: stringValue('media_type'),
    size: numberValue('size'),
    kind: stringValue('kind') || 'file',
    version: stringValue('version') || '',
    source: stringValue('source'),
    created_at: createdAt,
    timestamp: numberValue('timestamp') || createdAt,
    message: stringValue('message'),
    schema_version: numberValue('schema_version'),
    attachment,
  }
}

function attachmentFromUnknown(value: unknown): ArtifactAttachment | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const id = typeof record.id === 'string' ? record.id : ''
  const mediaType = typeof record.media_type === 'string' ? record.media_type : ''
  const size = typeof record.size === 'number' ? record.size : 0
  const width = typeof record.width === 'number' ? record.width : 0
  const height = typeof record.height === 'number' ? record.height : 0
  if (!id || !mediaType || size <= 0 || width <= 0 || height <= 0) return null
  return {
    id,
    media_type: mediaType,
    size,
    width,
    height,
    name: typeof record.name === 'string' ? record.name : undefined,
  }
}

function artifactListFromUnknown(value: unknown): ArtifactEvent[] {
  const candidates = Array.isArray(value)
    ? value
    : value && typeof value === 'object' && Array.isArray((value as { items?: unknown[] }).items)
      ? (value as { items: unknown[] }).items
      : []
  return candidates.map(artifactFromUnknown).filter((item): item is ArtifactEvent => Boolean(item))
}

function artifactKey(event: ArtifactEvent): string {
  return event.id || `${event.path}\u0000${event.version || event.timestamp || event.created_at || ''}`
}

function mergeArtifacts(current: ArtifactEvent[], incoming: ArtifactEvent[]): ArtifactEvent[] {
  const result: ArtifactEvent[] = []
  const seen = new Set<string>()
  const sorted = [...incoming, ...current].sort((left, right) => {
    const timeDifference = (right.created_at || right.timestamp || 0) - (left.created_at || left.timestamp || 0)
    return timeDifference || (right.sequence || 0) - (left.sequence || 0)
  })
  for (const item of sorted) {
    const key = artifactKey(item)
    if (seen.has(key)) continue
    seen.add(key)
    result.push(item)
    if (result.length >= 100) break
  }
  return result
}

function artifactTime(event: ArtifactEvent): string {
  const value = event.created_at || event.timestamp
  if (!value) return ''
  const millis = value < 10_000_000_000 ? value * 1000 : value
  try {
    return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(millis)
  } catch {
    return ''
  }
}

export function WorkspacePane({ session, onClose, focusPath = null, onFocusPathConsumed }: Props) {
  const [collapsed, setCollapsed] = useState(false)
  const [tab, setTab] = useState<PaneTab>('files')
  const [treeFilter, setTreeFilter] = useState('')
  const [paneWidth, setPaneWidth] = useState(780)
  const [treeWidth, setTreeWidth] = useState(292)
  const [grant, setGrant] = useState<FileGrant | null>(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState('')
  const [fileStale, setFileStale] = useState(false)
  const [artifacts, setArtifacts] = useState<ArtifactEvent[]>([])
  const [artifactsLoading, setArtifactsLoading] = useState(true)
  const [artifactsError, setArtifactsError] = useState('')
  const [eventConnection, setEventConnection] = useState<EventConnection>('connecting')
  const [workspaceConnection, setWorkspaceConnection] = useState<EventConnection>('connecting')
  const fileRequestRef = useRef(0)
  const treeButtonRefs = useRef(new Map<string, HTMLButtonElement>())
  const workspaceSocketRef = useRef<WebSocket | null>(null)
  const workspaceWatchPathsRef = useRef<string[]>([''])
  const selectedPathRef = useRef<string | null>(null)
  const tree = useWorkspaceTree(session.id, focusPath, onFocusPathConsumed)
  selectedPathRef.current = tree.selectedPath

  const loadArtifacts = useCallback(async (quiet = false) => {
    if (!quiet) setArtifactsLoading(true)
    setArtifactsError('')
    try {
      const payload = await api.artifacts(session.id)
      setArtifacts((current) => mergeArtifacts(current, artifactListFromUnknown(payload)))
    } catch (reason) {
      setArtifactsError(reason instanceof Error ? reason.message : '无法读取 Artifact 事件')
    } finally {
      setArtifactsLoading(false)
    }
  }, [session.id])

  useEffect(() => {
    setGrant(null)
    setFileError('')
    setArtifacts([])
    setCollapsed(false)
    void loadArtifacts()
  }, [loadArtifacts, session.id])

  useEffect(() => {
    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let refreshTimer: number | undefined
    let reconnectAttempt = 0

    const scheduleRefresh = (paths: string[]) => {
      window.clearTimeout(refreshTimer)
      refreshTimer = window.setTimeout(() => {
        tree.invalidatePaths(paths)
        void loadArtifacts(true)
      }, 250)
    }
    const connect = () => {
      if (disposed) return
      setEventConnection('connecting')
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/events/${encodeURIComponent(session.id)}`)
      socket.onopen = () => {
        reconnectAttempt = 0
        setEventConnection('online')
      }
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(String(message.data)) as unknown
          const incoming = Array.isArray(payload)
            ? payload.map(artifactFromUnknown).filter((item): item is ArtifactEvent => Boolean(item))
            : [artifactFromUnknown(payload)].filter((item): item is ArtifactEvent => Boolean(item))
          if (incoming.length) setArtifacts((current) => mergeArtifacts(current, incoming))
          if (incoming.length) scheduleRefresh(incoming.map((item) => item.path))
        } catch {
          // Unknown event versions are ignored; the HTTP refresh remains authoritative.
        }
      }
      socket.onclose = (event) => {
        setEventConnection('offline')
        if (disposed || event.code === 4401 || event.code === 4404) return
        const delay = Math.min(750 * 2 ** reconnectAttempt, 8000)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
      socket.onerror = () => socket?.close()
    }
    connect()
    return () => {
      disposed = true
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(refreshTimer)
      socket?.close(1000)
    }
  }, [loadArtifacts, session.id, tree.invalidatePaths])

  useEffect(() => {
    workspaceWatchPathsRef.current = tree.watchedPaths
    const socket = workspaceSocketRef.current
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'watch', paths: tree.watchedPaths }))
    }
  }, [tree.watchedPaths])

  useEffect(() => {
    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let reconnectAttempt = 0
    const connect = () => {
      if (disposed) return
      setWorkspaceConnection('connecting')
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/workspace/${encodeURIComponent(session.id)}`)
      workspaceSocketRef.current = socket
      socket.onopen = () => {
        reconnectAttempt = 0
        setWorkspaceConnection('online')
        socket?.send(JSON.stringify({ type: 'watch', paths: workspaceWatchPathsRef.current }))
      }
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(String(message.data)) as { type?: string; paths?: unknown }
          if (payload.type === 'changed' && Array.isArray(payload.paths)) {
            const paths = payload.paths.filter((path): path is string => typeof path === 'string')
            if (selectedPathRef.current && paths.includes(selectedPathRef.current)) setFileStale(true)
            tree.invalidatePaths(paths)
          }
        } catch {
          // Unknown workspace event versions are ignored.
        }
      }
      socket.onclose = (event) => {
        if (workspaceSocketRef.current === socket) workspaceSocketRef.current = null
        setWorkspaceConnection('offline')
        if (disposed || event.code === 4401 || event.code === 4404 || event.code === 4410) return
        const delay = Math.min(750 * 2 ** reconnectAttempt, 8000)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
      socket.onerror = () => socket?.close()
    }
    connect()
    return () => {
      disposed = true
      window.clearTimeout(reconnectTimer)
      if (workspaceSocketRef.current === socket) workspaceSocketRef.current = null
      socket?.close(1000)
    }
  }, [session.id, tree.invalidatePaths])

  const openFile = useCallback(async (path: string) => {
    const requestId = ++fileRequestRef.current
    tree.selectPath(path)
    setGrant(null)
    setFileLoading(true)
    setFileError('')
    setFileStale(false)
    try {
      const next = await api.resolveFile(session.id, path)
      if (requestId === fileRequestRef.current) setGrant(next)
    } catch (reason) {
      if (requestId === fileRequestRef.current) {
        setFileError(reason instanceof Error ? reason.message : '无法打开文件')
      }
    } finally {
      if (requestId === fileRequestRef.current) setFileLoading(false)
    }
  }, [session.id, tree.selectPath])

  const openArtifact = useCallback((artifact: ArtifactEvent) => {
    if (!artifact.attachment) {
      void openFile(artifact.path)
      return
    }
    fileRequestRef.current += 1
    const attachment = artifact.attachment
    const terminalId = artifact.terminal_id || session.id
    const name = attachment.name || artifact.name || artifact.path.split(/[\\/]/).filter(Boolean).pop() || 'artifact-image'
    tree.selectPath(artifact.path)
    setFileLoading(false)
    setFileError('')
    setFileStale(false)
    setGrant({
      id: attachment.id,
      terminal_id: terminalId,
      path: artifact.path,
      name,
      media_type: attachment.media_type,
      size: attachment.size,
      kind: 'file',
      version: artifact.version || attachment.id,
      etag: attachment.id,
      preview_mode: attachment.media_type.startsWith('image/') ? 'image' : 'download',
      inline_safe: attachment.media_type.startsWith('image/'),
      modified_at: artifact.created_at,
      expires_at: Number.MAX_SAFE_INTEGER,
      image_width: attachment.width,
      image_height: attachment.height,
      content_url: api.attachmentContentUrl(terminalId, attachment.id),
      immutable: true,
    })
  }, [openFile, session.id, tree.selectPath])

  const visibleTreeRows = useMemo(() => {
    const query = treeFilter.trim().toLocaleLowerCase()
    if (!query) return tree.rows
    return tree.rows.filter((row) => {
      if (row.type === 'more') return false
      const node = tree.nodes[row.path]
      return node?.name.toLocaleLowerCase().includes(query) || row.path.toLocaleLowerCase().includes(query)
    })
  }, [tree.nodes, tree.rows, treeFilter])
  const rootDirectory = tree.directories['']
  const listingLoading = rootDirectory?.status === 'loading'
  const previewOpen = Boolean(grant || fileLoading || fileError)

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem('agentserver:workspace-pane-size')
      if (!raw) return
      const value = JSON.parse(raw) as { pane?: number; tree?: number }
      if (Number.isFinite(value.pane)) setPaneWidth(Math.min(1100, Math.max(480, Number(value.pane))))
      if (Number.isFinite(value.tree)) setTreeWidth(Math.min(480, Math.max(220, Number(value.tree))))
    } catch {
      // Invalid or unavailable storage falls back to the default dimensions.
    }
  }, [])

  useEffect(() => {
    try {
      window.localStorage.setItem('agentserver:workspace-pane-size', JSON.stringify({ pane: paneWidth, tree: treeWidth }))
    } catch {
      // Resizing remains functional without persistence.
    }
  }, [paneWidth, treeWidth])

  const beginResize = useCallback((event: React.PointerEvent, target: 'pane' | 'tree') => {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = target === 'pane' ? paneWidth : treeWidth
    const previousCursor = document.body.style.cursor
    const previousSelection = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const move = (moveEvent: PointerEvent) => {
      if (target === 'pane') {
        const available = Math.max(480, window.innerWidth - 24)
        setPaneWidth(Math.min(1100, available, Math.max(480, startWidth + startX - moveEvent.clientX)))
      } else {
        setTreeWidth(Math.min(480, Math.max(220, startWidth + moveEvent.clientX - startX)))
      }
    }
    const stop = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousSelection
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop, { once: true })
  }, [paneWidth, treeWidth])

  const handleTreeKey = useCallback((event: React.KeyboardEvent, path: string, index: number) => {
    const row = visibleTreeRows[index]
    if (!row || row.type !== 'entry') return
    const node = tree.nodes[path]
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const direction = event.key === 'ArrowDown' ? 1 : -1
      for (let next = index + direction; next >= 0 && next < visibleTreeRows.length; next += direction) {
        const candidate = visibleTreeRows[next]
        if (candidate.type === 'entry') {
          treeButtonRefs.current.get(candidate.path)?.focus()
          break
        }
      }
      return
    }
    if (event.key === 'ArrowRight' && node?.kind === 'directory') {
      event.preventDefault()
      if (!tree.expanded[path]) tree.toggleDirectory(path)
      else {
        const child = tree.directories[path]?.children[0]
        if (child) treeButtonRefs.current.get(child)?.focus()
      }
      return
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      if (node?.kind === 'directory' && tree.expanded[path]) tree.toggleDirectory(path)
      else treeButtonRefs.current.get(parentWorkspacePath(path))?.focus()
      return
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      if (node?.kind === 'directory') tree.toggleDirectory(path)
      else if (node?.kind === 'file') void openFile(path)
    }
  }, [openFile, tree.directories, tree.expanded, tree.nodes, tree.toggleDirectory, visibleTreeRows])

  if (collapsed) {
    return (
      <aside className="grid h-full w-9 flex-none place-items-center border-l border-[#344b53] bg-[#0b1217]" aria-label={`${session.name} 工作区`}>
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          className="flex h-full w-full cursor-pointer flex-col items-center justify-center gap-2 text-[9px] text-primary hover:bg-[#122019]"
          aria-label="展开工作区"
          title="展开工作区"
        >
          <PanelRightOpen className="size-4" />
          <span className="[writing-mode:vertical-rl]">文件</span>
          {artifacts.length > 0 && <span className="grid min-h-4 min-w-4 place-items-center rounded-full bg-primary px-1 font-mono text-[8px] font-bold text-[#07120d]">{Math.min(artifacts.length, 99)}</span>}
        </button>
      </aside>
    )
  }

  return (
    <aside
      style={{ '--workspace-pane-width': `${paneWidth}px`, '--workspace-tree-width': `${treeWidth}px` } as React.CSSProperties}
      className="relative z-30 grid h-full w-full flex-none grid-rows-[48px_minmax(0,1fr)] overflow-hidden border-l border-[#344b53] bg-[#080d12] shadow-[-16px_0_40px_#0008] md:w-[var(--workspace-pane-width)] md:max-w-[88vw]"
      aria-label={`${session.name} 工作区`}
    >
      <div onPointerDown={(event) => beginResize(event, 'pane')} className="absolute inset-y-0 -left-1 z-40 hidden w-2 cursor-col-resize md:block" aria-hidden="true" />
      <header className="flex min-w-0 items-center gap-2 border-b border-[#26323c] bg-[#0d141a] px-3">
        <FolderOpen className="size-4 flex-none text-primary" />
        <div className="min-w-0 flex-1">
          <strong className="block truncate text-xs text-[#e2ebf1]">工作区</strong>
          <small className="block truncate font-mono text-[8px] text-[#657581]">{session.name} · {session.kind === 'ssh' ? session.device_name || '远程设备' : '本机'}</small>
        </div>
        <button type="button" onClick={() => tree.refreshDirectory('')} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-primary" aria-label="刷新工作区" title="刷新"><RefreshCw className={cn('size-3.5', listingLoading && 'animate-spin')} /></button>
        <button type="button" onClick={() => setCollapsed(true)} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-primary" aria-label="收起工作区" title="收起"><PanelRightClose className="size-3.5" /></button>
        <button type="button" onClick={onClose} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-white" aria-label="关闭工作区" title="关闭"><XIcon className="size-3.5" /></button>
      </header>

      <div className="grid min-h-0 grid-cols-1 md:grid-cols-[var(--workspace-tree-width)_4px_minmax(0,1fr)]">
        <section className={cn('grid min-h-0 grid-rows-[40px_minmax(0,1fr)] bg-[#0a1015]', previewOpen && 'max-md:hidden')}>
          <div className="grid grid-cols-2 border-b border-[#26323c] bg-[#0c1319] p-1">
            <button type="button" onClick={() => setTab('files')} className={cn('flex cursor-pointer items-center justify-center gap-1.5 rounded-md text-[10px] text-[#73838f] hover:text-[#cdd8df]', tab === 'files' && 'bg-[#17231d] text-primary')}><Folder className="size-3" />文件</button>
            <button type="button" onClick={() => setTab('artifacts')} className={cn('flex cursor-pointer items-center justify-center gap-1.5 rounded-md text-[10px] text-[#73838f] hover:text-[#cdd8df]', tab === 'artifacts' && 'bg-[#17231d] text-primary')}>
              <Activity className="size-3" />Artifacts
              {artifacts.length > 0 && <span className="rounded-full bg-[#25342d] px-1.5 font-mono text-[8px]">{artifacts.length}</span>}
            </button>
          </div>

          {tab === 'files' ? (
            <div className="grid min-h-0 grid-rows-[36px_30px_minmax(0,1fr)]">
              <div className="flex items-center border-b border-[#222e37] px-2">
                <input value={treeFilter} onChange={(event) => setTreeFilter(event.target.value)} aria-label="筛选工作区文件" spellCheck={false} className="h-6 min-w-0 flex-1 rounded border border-[#293640] bg-[#070b0f] px-2 font-mono text-[9px] text-[#cbd6de] outline-none placeholder:text-[#52606b] focus:border-[#47705e]" placeholder="筛选已加载的文件" />
              </div>
              <div className="flex min-w-0 items-center gap-1 border-b border-[#222e37] px-2 text-[9px] font-semibold uppercase tracking-wide text-[#a7b4bc]" title={tree.listing?.root || session.workspace?.root}>
                <ChevronDown className="size-3 flex-none" />
                <FolderOpen className="size-3 flex-none text-[#d2ad67]" />
                <span className="truncate">{(tree.listing?.root || session.workspace?.root || 'Workspace').split(/[\\/]/).filter(Boolean).pop() || 'Workspace'}</span>
                <Radio className={cn('ml-auto size-2.5 flex-none', workspaceConnection === 'online' ? 'text-primary' : workspaceConnection === 'offline' ? 'text-[#ed6876]' : 'text-[#e8b95e]')} aria-label={workspaceConnection === 'online' ? '文件变更监听已连接' : workspaceConnection === 'connecting' ? '正在连接文件变更监听' : '文件变更监听已断开'} />
              </div>
              <div role="tree" aria-label="工作区文件树" className="relative min-h-0 overflow-y-auto py-1 [scrollbar-width:thin]">
                {listingLoading && !tree.listing && <div className="grid h-full place-items-center text-[10px] text-[#768691]"><span className="flex items-center gap-2"><LoaderCircle className="size-3.5 animate-spin text-primary" />正在读取目录…</span></div>}
                {rootDirectory?.status === 'error' && (
                  <div className="m-3 rounded-lg border border-[#713640] bg-[#28171b99] p-3 text-[10px] leading-4 text-[#ffadb5]">
                    <span className="flex gap-2"><AlertTriangle className="mt-0.5 size-3.5 flex-none" />{rootDirectory.error}</span>
                    <button type="button" onClick={() => tree.refreshDirectory('')} className="mt-2 cursor-pointer text-[#ffc0c7] underline underline-offset-2">重试</button>
                  </div>
                )}
                {visibleTreeRows.map((row, index) => {
                  if (row.type === 'more') {
                    const loading = tree.directories[row.path]?.status === 'loading'
                    return <button key={`more:${row.path}`} type="button" onClick={() => tree.loadMore(row.path)} disabled={loading} style={{ paddingLeft: 22 + row.depth * 14 }} className="flex h-7 w-full cursor-pointer items-center gap-1.5 text-left text-[9px] text-primary hover:bg-[#142019] disabled:cursor-wait disabled:opacity-60">{loading && <LoaderCircle className="size-3 animate-spin" />}加载更多…</button>
                  }
                  const entry = tree.nodes[row.path]
                  if (!entry) return null
                  const directory = isDirectory(entry)
                  const expanded = directory && Boolean(tree.expanded[row.path])
                  const directoryState = directory ? tree.directories[row.path] : undefined
                  const Icon = directory ? (expanded ? FolderOpen : Folder) : entryIcon(entry)
                  const disabled = entry.kind === 'symlink' || entry.kind === 'other'
                  return (
                    <button
                      key={`${entry.kind}:${row.path}`}
                      ref={(element) => { if (element) treeButtonRefs.current.set(row.path, element); else treeButtonRefs.current.delete(row.path) }}
                      type="button"
                      role="treeitem"
                      aria-level={row.depth + 1}
                      aria-expanded={directory ? expanded : undefined}
                      aria-selected={tree.selectedPath === row.path}
                      data-tree-row={row.path}
                      disabled={disabled}
                      onKeyDown={(event) => handleTreeKey(event, row.path, index)}
                      onClick={() => directory ? tree.toggleDirectory(row.path) : void openFile(row.path)}
                      style={{ paddingLeft: 6 + row.depth * 14 }}
                      className={cn('group flex h-7 w-full cursor-pointer items-center gap-1 text-left text-[10px] outline-none hover:bg-[#151e25] focus-visible:bg-[#18242c] focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[#47705e]', tree.selectedPath === row.path && 'bg-[#193026] hover:bg-[#1b362a]', disabled && 'cursor-not-allowed opacity-45')}
                      title={disabled ? `${row.path}（不跟随符号链接或特殊文件）` : row.path}
                    >
                      <span className="grid size-3 flex-none place-items-center text-[#53636e]">{directory ? (expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />) : null}</span>
                      <Icon className={cn('size-3.5 flex-none text-[#6f8290]', directory && 'text-[#d2ad67]', !directory && entryIcon(entry) === FileImage && 'text-[#79c99f]')} />
                      <span className="min-w-0 flex-1 truncate text-[#c7d2da] group-hover:text-white">{entry.name}</span>
                      {!directory && entry.size !== undefined && entry.size !== null && <small className="flex-none pr-1 font-mono text-[7px] text-[#586772]">{formatBytes(entry.size)}</small>}
                      {directoryState?.status === 'loading' && <LoaderCircle className="mr-1 size-3 flex-none animate-spin text-primary" />}
                      {directoryState?.status === 'error' && <AlertTriangle className="mr-1 size-3 flex-none text-[#ff8995]" aria-label={directoryState.error} />}
                    </button>
                  )
                })}
                {!listingLoading && rootDirectory?.status === 'loaded' && !visibleTreeRows.length && <p className="m-0 px-3 py-8 text-center text-[10px] text-[#586873]">{treeFilter ? '没有匹配的已加载文件' : '工作区是空的'}</p>}
              </div>
            </div>
          ) : (
            <div className="grid min-h-0 grid-rows-[36px_minmax(0,1fr)]">
              <div className="flex items-center gap-2 border-b border-[#222e37] px-3 text-[9px] text-[#6f7f8a]">
                <Radio className={cn('size-3', eventConnection === 'online' ? 'text-primary' : eventConnection === 'offline' ? 'text-[#ed6876]' : 'text-[#e8b95e]')} />
                <span className="flex-1">{eventConnection === 'online' ? '实时事件已连接' : eventConnection === 'connecting' ? '正在连接事件流' : '事件流已断开，正在重试'}</span>
                <button type="button" onClick={() => void loadArtifacts()} className="grid size-6 cursor-pointer place-items-center rounded text-[#71818c] hover:bg-[#172129] hover:text-primary" title="刷新 Artifacts" aria-label="刷新 Artifacts"><RefreshCw className={cn('size-3', artifactsLoading && 'animate-spin')} /></button>
              </div>
              <div className="min-h-0 overflow-y-auto p-1.5 [scrollbar-width:thin]">
                {artifactsError && <div className="m-2 rounded-lg border border-[#713640] bg-[#28171b99] p-3 text-[10px] text-[#ffadb5]">{artifactsError}</div>}
                {artifacts.map((artifact) => {
                  const name = artifact.name || artifact.path.split(/[\\/]/).filter(Boolean).pop() || artifact.path
                  return (
                    <button key={artifactKey(artifact)} type="button" onClick={() => openArtifact(artifact)} className="group mb-1 flex min-h-12 w-full cursor-pointer items-start gap-2 rounded-lg border border-transparent px-2 py-2 text-left hover:border-[#2c3b45] hover:bg-[#121b22]" title={artifact.path}>
                      <span className="mt-0.5 grid size-7 flex-none place-items-center rounded-md border border-[#294336] bg-[#13231b] text-primary">{artifact.attachment ? <FileImage className="size-3.5" /> : <Activity className="size-3.5" />}</span>
                      <span className="min-w-0 flex-1">
                        <strong className="block truncate text-[10px] font-medium text-[#d5dfe6] group-hover:text-white">{name}</strong>
                        <small className="mt-0.5 block truncate font-mono text-[8px] text-[#61717c]">{artifact.path}</small>
                        {artifact.message && <small className="mt-0.5 block truncate text-[8px] text-[#81909b]">{artifact.message}</small>}
                      </span>
                      {artifact.attachment && <span className="mt-0.5 flex-none rounded border border-[#315a48] bg-[#13231b] px-1.5 py-0.5 text-[7px] font-semibold text-primary">不可变</span>}
                      <time className="mt-0.5 flex-none font-mono text-[8px] text-[#52616c]">{artifactTime(artifact)}</time>
                    </button>
                  )
                })}
                {!artifactsLoading && !artifactsError && !artifacts.length && <div className="grid h-full min-h-40 place-items-center px-6 text-center text-[10px] leading-5 text-[#5f6f7a]">Agent 产生的文件会实时出现在这里。</div>}
                {artifactsLoading && !artifacts.length && <div className="grid h-full min-h-40 place-items-center text-[10px] text-[#71818c]"><span className="flex items-center gap-2"><LoaderCircle className="size-3.5 animate-spin text-primary" />正在读取 Artifacts…</span></div>}
              </div>
            </div>
          )}
        </section>

        <div onPointerDown={(event) => beginResize(event, 'tree')} className="hidden cursor-col-resize border-x border-[#26323c] bg-[#111920] hover:bg-[#315a48] md:block" role="separator" aria-orientation="vertical" aria-label="调整文件树宽度" />

        <section className={cn('min-h-0 min-w-0 bg-[#070b0f] max-md:hidden', previewOpen && 'max-md:block')}>
          <ArtifactPreview
            grant={grant}
            resolving={fileLoading}
            resolveError={fileError}
            onRetry={tree.selectedPath && !grant?.immutable ? () => void openFile(tree.selectedPath as string) : undefined}
            onResolveDownload={grant && !grant.immutable ? (currentGrant) => api.resolveFile(session.id, currentGrant.path) : undefined}
            stale={fileStale && !grant?.immutable}
            onReload={tree.selectedPath ? () => void openFile(tree.selectedPath as string) : undefined}
            onClose={() => {
              fileRequestRef.current += 1
              setGrant(null)
              setFileLoading(false)
              setFileError('')
              setFileStale(false)
              tree.selectPath(null)
            }}
          />
        </section>
      </div>
    </aside>
  )
}
