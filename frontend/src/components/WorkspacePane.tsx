import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowUp,
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
  type WorkspaceBreadcrumb,
  type WorkspaceEntry,
  type WorkspaceListing,
} from '@/api'
import { ArtifactPreview } from '@/components/ArtifactPreview'
import { cn } from '@/lib/utils'

type Props = {
  session: TerminalSession
  onClose: () => void
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

function fallbackBreadcrumbs(listing: WorkspaceListing): WorkspaceBreadcrumb[] {
  if (listing.breadcrumbs?.length) return listing.breadcrumbs
  const path = listing.path || listing.root || ''
  const root = listing.root || ''
  if (!path) return [{ name: '工作区', path: '' }]

  const windows = /^[A-Za-z]:[\\/]/.test(path) || (path.includes('\\') && !path.includes('/'))
  const separator = windows ? '\\' : '/'
  let relative = path
  let base = ''
  if (root && path.toLowerCase().startsWith(root.toLowerCase())) {
    relative = path.slice(root.length).replace(/^[\\/]+/, '')
    base = root
  } else if (windows) {
    const drive = path.match(/^[A-Za-z]:[\\/]?/)?.[0] || ''
    base = drive.replace(/[\\/]*$/, '\\')
    relative = path.slice(drive.length)
  } else if (path.startsWith('/')) {
    base = '/'
    relative = path.slice(1)
  }

  const crumbs: WorkspaceBreadcrumb[] = [{ name: '工作区', path: base }]
  let accumulated = base
  for (const part of relative.split(/[\\/]+/).filter(Boolean)) {
    if (!accumulated) accumulated = part
    else if (accumulated.endsWith('/') || accumulated.endsWith('\\')) accumulated += part
    else accumulated += `${separator}${part}`
    crumbs.push({ name: part, path: accumulated })
  }
  return crumbs
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

export function WorkspacePane({ session, onClose }: Props) {
  const [collapsed, setCollapsed] = useState(false)
  const [tab, setTab] = useState<PaneTab>('files')
  const [listing, setListing] = useState<WorkspaceListing | null>(null)
  const [pathDraft, setPathDraft] = useState('')
  const [listingLoading, setListingLoading] = useState(true)
  const [listingError, setListingError] = useState('')
  const [selectedPath, setSelectedPath] = useState('')
  const [grant, setGrant] = useState<FileGrant | null>(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState('')
  const [artifacts, setArtifacts] = useState<ArtifactEvent[]>([])
  const [artifactsLoading, setArtifactsLoading] = useState(true)
  const [artifactsError, setArtifactsError] = useState('')
  const [eventConnection, setEventConnection] = useState<EventConnection>('connecting')
  const currentPathRef = useRef('')
  const listingRequestRef = useRef(0)
  const fileRequestRef = useRef(0)

  const loadWorkspace = useCallback(async (path: string, quiet = false) => {
    const requestId = ++listingRequestRef.current
    if (!quiet) setListingLoading(true)
    setListingError('')
    try {
      const next = await api.workspace(session.id, path)
      if (requestId !== listingRequestRef.current) return
      const resolvedPath = next.path || path
      currentPathRef.current = resolvedPath
      setListing(next)
      setPathDraft(resolvedPath)
    } catch (reason) {
      if (requestId !== listingRequestRef.current) return
      setListingError(reason instanceof Error ? reason.message : '无法读取工作区')
    } finally {
      if (requestId === listingRequestRef.current) setListingLoading(false)
    }
  }, [session.id])

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
    currentPathRef.current = ''
    setListing(null)
    setPathDraft('')
    setSelectedPath('')
    setGrant(null)
    setFileError('')
    setArtifacts([])
    setCollapsed(false)
    void loadWorkspace('')
    void loadArtifacts()
  }, [loadArtifacts, loadWorkspace, session.id])

  useEffect(() => {
    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let refreshTimer: number | undefined
    let reconnectAttempt = 0

    const scheduleRefresh = () => {
      window.clearTimeout(refreshTimer)
      refreshTimer = window.setTimeout(() => {
        void loadWorkspace(currentPathRef.current, true)
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
          scheduleRefresh()
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
  }, [loadArtifacts, loadWorkspace, session.id])

  const openFile = useCallback(async (path: string) => {
    const requestId = ++fileRequestRef.current
    setSelectedPath(path)
    setGrant(null)
    setFileLoading(true)
    setFileError('')
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
  }, [session.id])

  const openArtifact = useCallback((artifact: ArtifactEvent) => {
    if (!artifact.attachment) {
      void openFile(artifact.path)
      return
    }
    fileRequestRef.current += 1
    const attachment = artifact.attachment
    const terminalId = artifact.terminal_id || session.id
    const name = attachment.name || artifact.name || artifact.path.split(/[\\/]/).filter(Boolean).pop() || 'artifact-image'
    setSelectedPath(artifact.path)
    setFileLoading(false)
    setFileError('')
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
  }, [openFile, session.id])

  const entries = useMemo(() => {
    const values = listing?.entries || []
    return [...values].sort((left, right) => {
      const typeOrder = Number(isDirectory(right)) - Number(isDirectory(left))
      return typeOrder || left.name.localeCompare(right.name, 'zh-CN', { numeric: true, sensitivity: 'base' })
    })
  }, [listing])
  const breadcrumbs = useMemo(() => listing ? fallbackBreadcrumbs(listing) : [], [listing])
  const parentPath = listing?.parent_path ?? listing?.parent ?? (breadcrumbs.length > 1 ? breadcrumbs[breadcrumbs.length - 2].path : null)
  const previewOpen = Boolean(grant || fileLoading || fileError)

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className="absolute top-1/2 right-0 z-30 flex -translate-y-1/2 cursor-pointer flex-col items-center gap-2 rounded-l-xl border border-r-0 border-[#365047] bg-[#0c1814f2] px-2 py-3 text-[9px] text-primary shadow-[-10px_0_30px_#0009] backdrop-blur-md"
        aria-label="展开工作区"
        title="展开工作区"
      >
        <PanelRightOpen className="size-4" />
        <span className="[writing-mode:vertical-rl]">文件</span>
        {artifacts.length > 0 && <span className="grid min-h-4 min-w-4 place-items-center rounded-full bg-primary px-1 font-mono text-[8px] font-bold text-[#07120d]">{Math.min(artifacts.length, 99)}</span>}
      </button>
    )
  }

  return (
    <aside className="absolute inset-y-0 right-0 z-30 grid w-[min(780px,78vw)] grid-rows-[48px_minmax(0,1fr)] overflow-hidden border-l border-[#344b53] bg-[#080d12] shadow-[-24px_0_70px_#000c] max-md:inset-0 max-md:w-full max-md:border-l-0" aria-label={`${session.name} 工作区`}>
      <header className="flex min-w-0 items-center gap-2 border-b border-[#26323c] bg-[#0d141a] px-3">
        <FolderOpen className="size-4 flex-none text-primary" />
        <div className="min-w-0 flex-1">
          <strong className="block truncate text-xs text-[#e2ebf1]">工作区</strong>
          <small className="block truncate font-mono text-[8px] text-[#657581]">{session.name} · {session.kind === 'ssh' ? session.device_name || '远程设备' : '本机'}</small>
        </div>
        <button type="button" onClick={() => void loadWorkspace(currentPathRef.current)} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-primary" aria-label="刷新工作区" title="刷新"><RefreshCw className={cn('size-3.5', listingLoading && 'animate-spin')} /></button>
        <button type="button" onClick={() => setCollapsed(true)} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-primary" aria-label="收起工作区" title="收起"><PanelRightClose className="size-3.5" /></button>
        <button type="button" onClick={onClose} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-white" aria-label="关闭工作区" title="关闭"><XIcon className="size-3.5" /></button>
      </header>

      <div className="grid min-h-0 grid-cols-[292px_minmax(0,1fr)] max-md:grid-cols-1">
        <section className={cn('grid min-h-0 grid-rows-[40px_minmax(0,1fr)] border-r border-[#26323c] bg-[#0a1015] max-md:border-r-0', previewOpen && 'max-md:hidden')}>
          <div className="grid grid-cols-2 border-b border-[#26323c] bg-[#0c1319] p-1">
            <button type="button" onClick={() => setTab('files')} className={cn('flex cursor-pointer items-center justify-center gap-1.5 rounded-md text-[10px] text-[#73838f] hover:text-[#cdd8df]', tab === 'files' && 'bg-[#17231d] text-primary')}><Folder className="size-3" />文件</button>
            <button type="button" onClick={() => setTab('artifacts')} className={cn('flex cursor-pointer items-center justify-center gap-1.5 rounded-md text-[10px] text-[#73838f] hover:text-[#cdd8df]', tab === 'artifacts' && 'bg-[#17231d] text-primary')}>
              <Activity className="size-3" />Artifacts
              {artifacts.length > 0 && <span className="rounded-full bg-[#25342d] px-1.5 font-mono text-[8px]">{artifacts.length}</span>}
            </button>
          </div>

          {tab === 'files' ? (
            <div className="grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)]">
              <form className="flex gap-1.5 border-b border-[#222e37] p-2" onSubmit={(event) => { event.preventDefault(); void loadWorkspace(pathDraft) }}>
                <input value={pathDraft} onChange={(event) => setPathDraft(event.target.value)} aria-label="工作区路径" spellCheck={false} className="h-8 min-w-0 flex-1 rounded-md border border-[#2d3b46] bg-[#070b0f] px-2.5 font-mono text-[10px] text-[#cbd6de] outline-none placeholder:text-[#52606b] focus:border-[#47705e]" placeholder="输入目录路径" />
                <button type="submit" className="h-8 cursor-pointer rounded-md border border-[#34404b] bg-[#141c23] px-2.5 text-[9px] text-[#a9b6bf] hover:border-[#47705e] hover:text-primary">前往</button>
              </form>
              <nav aria-label="路径面包屑" className="flex min-w-0 items-center gap-0.5 overflow-x-auto border-b border-[#222e37] px-2 py-1.5 [scrollbar-width:thin]">
                {breadcrumbs.map((crumb, index) => (
                  <span key={`${crumb.path}-${index}`} className="flex flex-none items-center">
                    {index > 0 && <ChevronRight className="size-3 text-[#45545f]" />}
                    <button type="button" onClick={() => void loadWorkspace(crumb.path)} className={cn('max-w-[140px] cursor-pointer truncate rounded px-1.5 py-1 text-[9px] text-[#80909b] hover:bg-[#172129] hover:text-primary', index === breadcrumbs.length - 1 && 'text-[#c4d0d8]')} title={crumb.path}>{crumb.name}</button>
                  </span>
                ))}
              </nav>
              <div className="relative min-h-0 overflow-y-auto [scrollbar-width:thin]">
                {listingLoading && !listing && <div className="grid h-full place-items-center text-[10px] text-[#768691]"><span className="flex items-center gap-2"><LoaderCircle className="size-3.5 animate-spin text-primary" />正在读取目录…</span></div>}
                {listingError && (
                  <div className="m-3 rounded-lg border border-[#713640] bg-[#28171b99] p-3 text-[10px] leading-4 text-[#ffadb5]">
                    <span className="flex gap-2"><AlertTriangle className="mt-0.5 size-3.5 flex-none" />{listingError}</span>
                    <button type="button" onClick={() => void loadWorkspace(currentPathRef.current)} className="mt-2 cursor-pointer text-[#ffc0c7] underline underline-offset-2">重试</button>
                  </div>
                )}
                {!listingError && listing && (
                  <div className="p-1.5">
                    {parentPath !== null && parentPath !== undefined && (
                      <button type="button" onClick={() => void loadWorkspace(parentPath)} className="flex h-9 w-full cursor-pointer items-center gap-2 rounded-md px-2 text-left text-[10px] text-[#8c9ba6] hover:bg-[#151e25] hover:text-primary"><ArrowUp className="size-3.5" /><span>上一级</span></button>
                    )}
                    {entries.map((entry) => {
                      const Icon = entryIcon(entry)
                      const directory = isDirectory(entry)
                      return (
                        <button key={`${entry.kind}:${entry.path}`} type="button" onClick={() => directory ? void loadWorkspace(entry.path) : void openFile(entry.path)} className="group flex min-h-10 w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-[#151e25]" title={entry.path}>
                          <Icon className={cn('size-3.5 flex-none text-[#6f8290]', directory && 'text-[#d2ad67]', !directory && entryIcon(entry) === FileImage && 'text-[#79c99f]')} />
                          <span className="min-w-0 flex-1 truncate text-[10px] text-[#c7d2da] group-hover:text-white">{entry.name}</span>
                          {!directory && entry.size !== undefined && entry.size !== null && <small className="flex-none font-mono text-[8px] text-[#586772]">{formatBytes(entry.size)}</small>}
                          {directory && <ChevronRight className="size-3 flex-none text-[#4f5f6b]" />}
                        </button>
                      )
                    })}
                    {!entries.length && <p className="m-0 px-3 py-8 text-center text-[10px] text-[#586873]">这个目录是空的</p>}
                    {listing.truncated && <p className="m-1 rounded-md border border-[#5f4d29] bg-[#2a2315] px-2.5 py-2 text-[9px] text-[#e9c36e]">目录内容过多，仅显示部分条目。</p>}
                  </div>
                )}
                {listingLoading && listing && <div className="absolute top-1.5 right-2 rounded bg-[#111a21dd] p-1.5"><LoaderCircle className="size-3 animate-spin text-primary" /></div>}
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

        <section className={cn('min-h-0 min-w-0 bg-[#070b0f] max-md:hidden', previewOpen && 'max-md:block')}>
          <ArtifactPreview
            grant={grant}
            resolving={fileLoading}
            resolveError={fileError}
            onRetry={selectedPath && !grant?.immutable ? () => void openFile(selectedPath) : undefined}
            onResolveDownload={grant && !grant.immutable ? (currentGrant) => api.resolveFile(session.id, currentGrant.path) : undefined}
            onClose={() => {
              fileRequestRef.current += 1
              setGrant(null)
              setFileLoading(false)
              setFileError('')
              setSelectedPath('')
            }}
          />
        </section>
      </div>
    </aside>
  )
}
