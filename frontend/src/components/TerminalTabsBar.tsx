import { useEffect, useMemo, useRef, useState } from 'react'
import { Columns2, CornerDownLeft, LoaderCircle, Maximize2, MonitorPlay, Plus, Search } from 'lucide-react'

import type { Device, TerminalSession } from '@/api'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import {
  buildTerminalDisplayMap,
  groupTerminalSessions,
  type TerminalDisplay,
} from '@/terminal-display'
import { listLeaves, type LayoutNode } from '@/terminal-layout'

export { groupTerminalSessions } from '@/terminal-display'
export type { TerminalGroup } from '@/terminal-display'

type Placement = {
  pane: number | null
  visible: boolean
  focused: boolean
}

type Props = {
  devices: Device[]
  sessions: TerminalSession[]
  activeId: string | null
  cloningId: string | null
  layout: LayoutNode | null
  focusedLeafId: string | null
  focusMode: boolean
  coarseMode: boolean
  findTabRequest?: { requestId: number; targetLeafId: string } | null
  onFindTabRequestHandled?: (requestId: number) => void
  onToggleFocusMode: () => void
  onSelect: (id: string, targetLeafId?: string) => void
  onClone: (source: TerminalSession) => void
  onPreview: (source: TerminalSession) => void
}

function normalizedTokens(query: string): string[] {
  return query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
}

export function TerminalTabsBar({
  devices,
  sessions,
  activeId,
  cloningId,
  layout,
  focusedLeafId,
  focusMode,
  coarseMode,
  findTabRequest,
  onFindTabRequestHandled,
  onToggleFocusMode,
  onSelect,
  onClone,
  onPreview,
}: Props) {
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchIntent, setSearchIntent] = useState<
    { mode: 'reveal' } | { mode: 'attach-unassigned'; targetLeafId: string }
  >({ mode: 'reveal' })
  const [query, setQuery] = useState('')
  const [highlightedSessionId, setHighlightedSessionId] = useState<string | null>(null)
  const pendingFocusSessionRef = useRef<string | null>(null)
  const groupRefs = useRef(new Map<string, HTMLElement>())
  const resultRefs = useRef(new Map<string, HTMLButtonElement>())
  const deviceMap = useMemo(() => new Map(devices.map((device) => [device.id, device])), [devices])
  const terminalGroups = useMemo(() => groupTerminalSessions(sessions).sort((first, second) => {
    if (first.deviceId === null) return second.deviceId === null ? 0 : -1
    if (second.deviceId === null) return 1
    const firstName = deviceMap.get(first.deviceId)?.name || first.name
    const secondName = deviceMap.get(second.deviceId)?.name || second.name
    return firstName.localeCompare(secondName, 'zh-CN', { numeric: true, sensitivity: 'base' }) ||
      first.deviceId.localeCompare(second.deviceId)
  }), [deviceMap, sessions])
  const baseDisplayMap = useMemo(() => buildTerminalDisplayMap(sessions), [sessions])
  const leaves = useMemo(() => layout ? listLeaves(layout) : [], [layout])
  const assignedSessionIds = useMemo(
    () => new Set(leaves.flatMap((leaf) => leaf.tabs)),
    [leaves],
  )
  const singleVisibleSession = coarseMode || focusMode

  const placements = useMemo(() => {
    const result = new Map<string, Placement>()
    leaves.forEach((leaf, leafIndex) => {
      leaf.tabs.forEach((sessionId) => {
        const visible = singleVisibleSession
          ? sessionId === activeId
          : leaf.activeTab === sessionId
        result.set(sessionId, {
          pane: leafIndex + 1,
          visible,
          focused: sessionId === activeId && (singleVisibleSession || leaf.id === focusedLeafId),
        })
      })
    })
    if (activeId && !result.has(activeId)) {
      result.set(activeId, { pane: null, visible: true, focused: true })
    }
    return result
  }, [activeId, focusedLeafId, leaves, singleVisibleSession])
  const focusedGroupKey = terminalGroups.find((group) => (
    group.sessions.some((session) => placements.get(session.id)?.focused)
  ))?.key || null
  const displayMap = useMemo(() => {
    const result = new Map<string, TerminalDisplay>()
    sessions.forEach((session) => {
      const base = baseDisplayMap.get(session.id)
      if (!base) return
      const device = session.device_id ? deviceMap.get(session.device_id) : undefined
      const groupName = session.device_id ? device?.name || base.groupName : '本机'
      const deviceText = device
        ? [device.id, device.name, device.hostname, device.client_ip, device.notes, device.proxy_name].join(' ')
        : '本机 local localhost'
      result.set(session.id, {
        ...base,
        groupName,
        searchText: `${base.searchText} ${groupName} ${deviceText}`.toLocaleLowerCase(),
      })
    })
    return result
  }, [baseDisplayMap, deviceMap, sessions])

  const searchResults = useMemo(() => {
    const tokens = normalizedTokens(query)
    const availableSessions = searchIntent.mode === 'attach-unassigned'
      ? sessions.filter((session) => !assignedSessionIds.has(session.id))
      : sessions
    const filtered = tokens.length
      ? availableSessions.filter((session) => {
          const text = displayMap.get(session.id)?.searchText || ''
          return tokens.every((token) => text.includes(token))
        })
      : [...availableSessions]
    return filtered.sort((first, second) => {
      if (first.id === activeId) return -1
      if (second.id === activeId) return 1
      const firstVisible = placements.get(first.id)?.visible ? 1 : 0
      const secondVisible = placements.get(second.id)?.visible ? 1 : 0
      if (firstVisible !== secondVisible) return secondVisible - firstVisible
      if (first.active !== second.active) return Number(second.active) - Number(first.active)
      return second.created_at - first.created_at
    })
  }, [activeId, assignedSessionIds, displayMap, placements, query, searchIntent, sessions])
  const highlightedIndex = Math.max(0, searchResults.findIndex((session) => session.id === highlightedSessionId))
  const searchResultKey = searchResults.map((session) => session.id).join('\u0000')

  const openSearch = (
    initialQuery = '',
    intent: { mode: 'reveal' } | { mode: 'attach-unassigned'; targetLeafId: string } = { mode: 'reveal' },
  ) => {
    setSearchIntent(intent)
    setQuery(initialQuery)
    setHighlightedSessionId(activeId)
    setSearchOpen(true)
  }
  const chooseSession = (session: TerminalSession) => {
    pendingFocusSessionRef.current = session.id
    setSearchOpen(false)
    setQuery('')
    setHighlightedSessionId(null)
    onSelect(
      session.id,
      searchIntent.mode === 'attach-unassigned' ? searchIntent.targetLeafId : undefined,
    )
  }

  useEffect(() => {
    if (!findTabRequest) return
    openSearch('', { mode: 'attach-unassigned', targetLeafId: findTabRequest.targetLeafId })
    onFindTabRequestHandled?.(findTabRequest.requestId)
    // requestId is an event token: repeated clicks for the same leaf must reopen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findTabRequest?.requestId])

  useEffect(() => {
    if (!focusedGroupKey) return
    const frame = window.requestAnimationFrame(() => {
      groupRefs.current.get(focusedGroupKey)?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [focusedGroupKey])

  useEffect(() => {
    if (!searchOpen) return
    setHighlightedSessionId((current) => {
      if (current && searchResults.some((session) => session.id === current)) return current
      return searchResults.find((session) => session.id === activeId)?.id || searchResults[0]?.id || null
    })
  }, [activeId, searchOpen, searchResultKey])

  useEffect(() => {
    if (!searchOpen) return
    const highlighted = searchResults.find((session) => session.id === highlightedSessionId)
    if (!highlighted) return
    resultRefs.current.get(highlighted.id)?.scrollIntoView({ block: 'nearest' })
  }, [highlightedSessionId, searchOpen, searchResultKey])

  const canToggleFocusMode = !coarseMode && leaves.length > 1
  const targetPaneNumber = searchIntent.mode === 'attach-unassigned'
    ? leaves.findIndex((leaf) => leaf.id === searchIntent.targetLeafId) + 1
    : 0

  return (
    <>
      <nav aria-label="终端导航" className="flex min-w-0 items-center border-b border-border bg-[#0b1016] px-2 py-[5px] max-md:px-1.5">
        <button
          type="button"
          onClick={() => openSearch('', { mode: 'reveal' })}
          aria-label="搜索终端"
          title="搜索设备和终端"
          className="mr-1.5 flex h-[33px] flex-none cursor-pointer items-center gap-1.5 rounded-[7px] border border-[#2a3741] bg-[#10171e] px-2.5 text-[9px] font-semibold text-[#91a0ab] hover:border-[#3c5d4d] hover:bg-[#15231d] hover:text-primary max-md:mr-1 max-md:w-8 max-md:justify-center max-md:px-0"
        >
          <Search className="size-3.5" />
          <span className="max-md:hidden">搜索</span>
        </button>

        <div
          role="list"
          aria-label="终端设备组"
          className="flex min-w-0 flex-1 touch-pan-x items-center gap-1.5 overflow-x-auto overscroll-x-contain [scrollbar-color:#34404c_transparent] [scrollbar-width:thin]"
        >
          {terminalGroups.map((group) => {
            const device = group.deviceId ? deviceMap.get(group.deviceId) : undefined
            const groupName = group.deviceId ? device?.name || group.name : '本机'
            const cloneSource = group.sessions.find((session) => session.id === activeId) ||
              [...group.sessions].reverse().find((session) => session.active) ||
              group.sessions[group.sessions.length - 1]
            const runningCount = group.sessions.filter((session) => session.active).length
            const visibleCount = group.sessions.filter((session) => placements.get(session.id)?.visible).length
            const focusedSession = group.sessions.find((session) => placements.get(session.id)?.focused)
            const focusedPlacement = focusedSession ? placements.get(focusedSession.id) : undefined
            const groupFocused = Boolean(focusedSession)
            const groupVisible = visibleCount > 0
            const focusSummary = groupFocused
              ? `，当前焦点${focusedPlacement?.pane ? `在窗格 ${focusedPlacement.pane}` : ''}`
              : ''
            const groupSummary = `${group.sessions.length} 个终端，${runningCount} 个运行中，${visibleCount} 个可见${focusSummary}`
            return (
              <section
                key={group.key}
                ref={(node) => {
                  if (node) groupRefs.current.set(group.key, node)
                  else groupRefs.current.delete(group.key)
                }}
                role="listitem"
                className={cn(
                  'flex h-[33px] flex-none items-stretch overflow-hidden rounded-[7px] border border-[#26323c] bg-[#0f161d] hover:border-[#354451]',
                  groupVisible && 'border-[#344d42]',
                  groupFocused && 'border-[#315a48] bg-[#122019] shadow-[inset_0_-2px_var(--color-primary)]',
                )}
              >
                <button
                  type="button"
                  aria-label={`查找 ${groupName} 的终端，${groupSummary}`}
                  onClick={() => openSearch(groupName, { mode: 'reveal' })}
                  title={`${device ? `${device.name} · ${device.hostname || device.id}` : '本机终端'} · ${groupSummary}`}
                  className="flex min-w-[185px] max-w-[280px] cursor-pointer items-center gap-2 border-r border-[#26323c] px-2.5 text-left hover:bg-[#17212a] focus-visible:bg-[#17212a] max-md:min-w-[158px] max-md:max-w-[210px] max-md:px-2"
                >
                  <i
                    aria-hidden="true"
                    data-running={runningCount > 0}
                    className="size-1.5 flex-none rounded-full bg-[#59636d] data-[running=true]:bg-primary data-[running=true]:shadow-[0_0_8px_#77f2b488]"
                  />
                  <span className="min-w-0 flex-1">
                    <strong className={cn('block truncate text-[10px] leading-[12px] font-semibold text-[#b9c5ce] max-md:text-[9px]', groupFocused && 'text-[#edf4f9]')}>
                      {groupName}
                    </strong>
                    <span aria-hidden="true" className="mt-px flex items-center gap-1.5 whitespace-nowrap font-mono text-[7px] leading-[10px] text-[#687782]">
                      <span>{group.sessions.length} 终端</span>
                      <span className={cn(runningCount > 0 && 'text-[#8fc9ac]')}>{runningCount} 运行</span>
                      <span className={cn(groupVisible && 'text-[#91c3aa]')}>{visibleCount} 可见</span>
                      {groupFocused && (
                        <span className="rounded bg-primary px-1 font-bold text-[#07120d]">
                          焦点{focusedPlacement?.pane ? ` P${focusedPlacement.pane}` : ''}
                        </span>
                      )}
                    </span>
                  </span>
                  <Search aria-hidden="true" className="size-3 flex-none text-[#5f6e79]" />
                </button>
                {group.deviceId && (
                  <button
                    type="button"
                    aria-label={`预览 ${groupName} 的开发服务`}
                    title="预览设备本地开发服务"
                    onClick={() => onPreview(cloneSource)}
                    className="grid w-8 flex-none cursor-pointer place-items-center border-r border-[#26323c] bg-transparent text-[#709b88] hover:bg-[#193025] hover:text-primary focus-visible:bg-[#193025]"
                  >
                    <MonitorPlay className="size-3.5" />
                  </button>
                )}
                <button
                  type="button"
                  disabled={cloningId !== null}
                  aria-label={`在 ${groupName} 新建终端`}
                  title={group.deviceId ? '在同一设备新建终端' : '新建本地终端'}
                  onClick={() => onClone(cloneSource)}
                  className="grid w-8 flex-none cursor-pointer place-items-center bg-transparent text-[#709b88] hover:bg-[#193025] hover:text-primary focus-visible:bg-[#193025] disabled:cursor-wait disabled:text-[#536159] disabled:hover:bg-transparent"
                >
                  {cloningId && group.sessions.some((session) => session.id === cloningId)
                    ? <LoaderCircle className="size-3 animate-spin" />
                    : <Plus className="size-3.5" />}
                </button>
              </section>
            )
          })}
        </div>

        {canToggleFocusMode && (
          <button
            type="button"
            aria-label={focusMode ? '恢复分屏' : '聚焦当前终端'}
            title={focusMode ? '恢复分屏' : '仅显示当前终端'}
            onClick={onToggleFocusMode}
            className={cn(
              'ml-1.5 flex h-[33px] flex-none cursor-pointer items-center gap-1.5 rounded-[7px] border border-[#2a3741] bg-[#10171e] px-2.5 text-[9px] font-semibold text-[#91a0ab] hover:border-[#3c5d4d] hover:text-primary',
              focusMode && 'border-[#315a48] bg-[#17271f] text-primary',
            )}
          >
            {focusMode ? <Columns2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
            <span>{focusMode ? '恢复分屏' : '聚焦当前'}</span>
          </button>
        )}
      </nav>

      <Dialog
        open={searchOpen}
        onOpenChange={(open) => {
          setSearchOpen(open)
          if (!open) {
            setQuery('')
            setSearchIntent({ mode: 'reveal' })
          }
        }}
      >
        <DialogContent
          className="grid max-h-[min(720px,calc(100dvh-2rem))] grid-rows-[auto_auto_minmax(0,1fr)_auto] gap-0 overflow-hidden p-0 sm:max-w-[720px]"
          onCloseAutoFocus={(event) => {
            const sessionId = pendingFocusSessionRef.current
            if (!sessionId) return
            pendingFocusSessionRef.current = null
            // 避免 Radix 把焦点送回搜索按钮。App 会用带竞态保护的
            // focusTerminalInput 恢复键盘输入；这里不再无条件抢走用户新焦点。
            event.preventDefault()
          }}
        >
          <DialogHeader className="border-b border-[#293641] px-5 pt-5 pb-4 pr-12">
            <DialogTitle className="text-base">
              {searchIntent.mode === 'attach-unassigned'
                ? `向窗格 ${targetPaneNumber > 0 ? targetPaneNumber : ''} 添加终端`
                : '查找终端'}
            </DialogTitle>
            <DialogDescription className="text-[10px]">
              {searchIntent.mode === 'attach-unassigned'
                ? '仅显示尚未归属任何窗格的后台终端，不会暗中移动其他窗格的标签。'
                : '搜索设备、终端、工作区、服务端口或运行状态。'}
            </DialogDescription>
          </DialogHeader>
          <div className="relative border-b border-[#26323c] p-3">
            <Search className="pointer-events-none absolute top-1/2 left-6 size-4 -translate-y-1/2 text-[#657580]" />
            <Input
              autoFocus
              role="combobox"
              aria-label="搜索设备、终端、工作区或服务"
              aria-autocomplete="list"
              aria-expanded="true"
              aria-controls="terminal-search-results"
              aria-activedescendant={searchResults[highlightedIndex] ? `terminal-result-${searchResults[highlightedIndex].id}` : undefined}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
                if (!searchResults.length) return
                if (event.key === 'ArrowDown') {
                  event.preventDefault()
                  setHighlightedSessionId(searchResults[(highlightedIndex + 1) % searchResults.length].id)
                } else if (event.key === 'ArrowUp') {
                  event.preventDefault()
                  setHighlightedSessionId(searchResults[(highlightedIndex - 1 + searchResults.length) % searchResults.length].id)
                } else if (event.key === 'Enter') {
                  event.preventDefault()
                  const selected = searchResults[highlightedIndex]
                  if (selected) chooseSession(selected)
                }
              }}
              placeholder="例如：设备名、hostname、项目目录、3000、运行中"
              className="h-11 pl-10 text-xs"
            />
          </div>
          <div id="terminal-search-results" role="listbox" aria-label="终端搜索结果" className="min-h-0 overflow-y-auto p-2">
            {searchResults.map((session, index) => {
              const display = displayMap.get(session.id)
              const placement = placements.get(session.id)
              const highlighted = index === highlightedIndex
              return (
                <button
                  id={`terminal-result-${session.id}`}
                  ref={(node) => {
                    if (node) resultRefs.current.set(session.id, node)
                    else resultRefs.current.delete(session.id)
                  }}
                  key={session.id}
                  type="button"
                  role="option"
                  tabIndex={-1}
                  aria-selected={highlighted}
                  onMouseMove={() => setHighlightedSessionId(session.id)}
                  onClick={() => chooseSession(session)}
                  className={cn(
                    'flex w-full cursor-pointer items-center gap-3 rounded-lg border border-transparent px-3 py-2.5 text-left hover:bg-[#17212a]',
                    highlighted && 'border-[#315a48] bg-[#17251f]',
                  )}
                >
                  <span className={cn('size-2 flex-none rounded-full bg-[#59636d]', session.active && 'bg-primary shadow-[0_0_8px_#77f2b466]')} />
                  <span className="min-w-0 flex-1">
                    <strong className="block truncate text-[11px] font-semibold text-[#dbe5eb]">{display?.groupName || session.device_name || '本机'} / {display?.label || session.name}</strong>
                    <small className="mt-1 block truncate font-mono text-[8px] text-[#71808b]">{display?.workspaceLabel || session.cwd} · {display?.shortId || session.id.slice(0, 8)}</small>
                  </span>
                  <span className="flex flex-none flex-wrap justify-end gap-1 text-[7px]">
                    <span className={cn('rounded border px-1.5 py-1', session.active ? 'border-[#315a48] bg-[#14251d] text-primary' : 'border-[#3a3337] bg-[#21181b] text-[#c47b83]')}>{session.active ? '运行中' : '已退出'}</span>
                    {placement?.pane && <span className="rounded border border-[#34414b] bg-[#151d24] px-1.5 py-1 text-[#94a1aa]">窗格 {placement.pane}</span>}
                    {placement?.visible && <span className="rounded border border-[#355848] bg-[#14241c] px-1.5 py-1 text-[#9ed7ba]">可见</span>}
                    {placement?.focused && <span className="rounded border border-primary/50 bg-primary px-1.5 py-1 font-bold text-[#07120d]">当前焦点</span>}
                  </span>
                </button>
              )
            })}
            {!searchResults.length && (
              <div className="px-4 py-12 text-center text-[11px] text-[#65747f]">
                {searchIntent.mode === 'attach-unassigned'
                  ? '没有可添加的未归属终端，可先在设备组中新建。'
                  : '没有匹配的终端'}
              </div>
            )}
          </div>
          <footer className="flex items-center justify-between border-t border-[#26323c] px-4 py-2 font-mono text-[8px] text-[#61707a]">
            <span>{searchResults.length} 个终端</span>
            <span className="flex items-center gap-2"><span>↑↓ 选择</span><span className="flex items-center gap-1"><CornerDownLeft className="size-3" />打开</span><span>Esc 关闭</span></span>
          </footer>
        </DialogContent>
      </Dialog>
    </>
  )
}
