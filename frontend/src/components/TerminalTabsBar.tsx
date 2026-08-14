import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Columns2, CornerDownLeft, LoaderCircle, Maximize2, MonitorPlay, Plus, Search, XIcon } from 'lucide-react'

import type { Device, TerminalSession } from '@/api'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { getCharacter, hashString, type CharPose } from '@/pixel/sprites'
import {
  buildTerminalDisplayMap,
  groupTerminalSessions,
  type TerminalDisplay,
} from '@/terminal-display'
import { listLeaves, type LayoutNode } from '@/terminal-layout'

export { groupTerminalSessions } from '@/terminal-display'
export type { TerminalGroup } from '@/terminal-display'

function SessionSprite({ session, pose }: { session: TerminalSession; pose: CharPose }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    const context = canvas?.getContext('2d')
    if (!canvas || !context) return
    const sprite = getCharacter(pose, hashString(session.id), 0, false, session.active)
    const scale = Math.min(canvas.width / sprite.width, canvas.height / sprite.height)
    const width = Math.floor(sprite.width * scale)
    const height = Math.floor(sprite.height * scale)
    context.imageSmoothingEnabled = false
    context.clearRect(0, 0, canvas.width, canvas.height)
    context.drawImage(sprite, Math.floor((canvas.width - width) / 2), canvas.height - height, width, height)
  }, [session.id, session.active, pose])
  return <canvas ref={ref} width={18} height={24} aria-hidden="true" className="my-auto flex-none" />
}

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
  onToggleFocusMode: () => void
  onSelect: (id: string) => void
  onClose: (session: TerminalSession) => void
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
  onToggleFocusMode,
  onSelect,
  onClose,
  onClone,
  onPreview,
}: Props) {
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [highlightedSessionId, setHighlightedSessionId] = useState<string | null>(null)
  const [expandedGroupKeys, setExpandedGroupKeys] = useState<Set<string>>(() => new Set())
  const pendingFocusSessionRef = useRef<string | null>(null)
  const tabRefs = useRef(new Map<string, HTMLButtonElement>())
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
  const visibleGroupKey = terminalGroups
    .filter((group) => group.sessions.some((session) => placements.get(session.id)?.visible))
    .map((group) => group.key)
    .join('\u0000')

  useEffect(() => {
    if (!visibleGroupKey) return
    const visibleKeys = visibleGroupKey.split('\u0000')
    setExpandedGroupKeys((current) => {
      if (visibleKeys.every((key) => current.has(key))) return current
      const next = new Set(current)
      for (const key of visibleKeys) next.add(key)
      return next
    })
  }, [visibleGroupKey])

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
    const filtered = tokens.length
      ? sessions.filter((session) => {
          const text = displayMap.get(session.id)?.searchText || ''
          return tokens.every((token) => text.includes(token))
        })
      : [...sessions]
    return filtered.sort((first, second) => {
      if (first.id === activeId) return -1
      if (second.id === activeId) return 1
      const firstVisible = placements.get(first.id)?.visible ? 1 : 0
      const secondVisible = placements.get(second.id)?.visible ? 1 : 0
      if (firstVisible !== secondVisible) return secondVisible - firstVisible
      if (first.active !== second.active) return Number(second.active) - Number(first.active)
      return second.created_at - first.created_at
    })
  }, [activeId, displayMap, placements, query, sessions])
  const highlightedIndex = Math.max(0, searchResults.findIndex((session) => session.id === highlightedSessionId))
  const searchResultKey = searchResults.map((session) => session.id).join('\u0000')

  const openSearch = (initialQuery = '') => {
    setQuery(initialQuery)
    setHighlightedSessionId(activeId)
    setSearchOpen(true)
  }
  const chooseSession = (session: TerminalSession) => {
    pendingFocusSessionRef.current = session.id
    setSearchOpen(false)
    setQuery('')
    setHighlightedSessionId(null)
    onSelect(session.id)
  }

  useEffect(() => {
    if (!activeId) return
    const frame = window.requestAnimationFrame(() => {
      tabRefs.current.get(activeId)?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [activeId, expandedGroupKeys])

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

  return (
    <>
      <nav aria-label="终端导航" className="flex min-w-0 items-center border-b border-border bg-[#0b1016] px-2 py-[5px] max-md:px-1.5">
        <button
          type="button"
          onClick={() => openSearch()}
          aria-label="搜索终端"
          title="搜索设备和终端"
          className="mr-1.5 flex h-[33px] flex-none cursor-pointer items-center gap-1.5 rounded-[7px] border border-[#2a3741] bg-[#10171e] px-2.5 text-[9px] font-semibold text-[#91a0ab] hover:border-[#3c5d4d] hover:bg-[#15231d] hover:text-primary max-md:mr-1 max-md:w-8 max-md:justify-center max-md:px-0"
        >
          <Search className="size-3.5" />
          <span className="max-md:hidden">搜索</span>
        </button>

        <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto [scrollbar-color:#34404c_transparent] [scrollbar-width:thin]">
          {terminalGroups.map((group) => {
            const device = group.deviceId ? deviceMap.get(group.deviceId) : undefined
            const groupName = group.deviceId ? device?.name || group.name : '本机'
            const cloneSource = group.sessions.find((session) => session.id === activeId) ||
              [...group.sessions].reverse().find((session) => session.active) ||
              group.sessions[group.sessions.length - 1]
            const groupFocused = group.sessions.some((session) => placements.get(session.id)?.focused)
            const groupVisible = group.sessions.some((session) => placements.get(session.id)?.visible)
            const groupExpanded = expandedGroupKeys.has(group.key)
            const importantSessions = group.sessions.filter((session) => (
              session.id === activeId || placements.get(session.id)?.visible
            ))
            const displayedSessions = [...importantSessions]
            for (const session of [...group.sessions].reverse()) {
              if (displayedSessions.length >= Math.max(8, importantSessions.length)) break
              if (!displayedSessions.some((item) => item.id === session.id)) displayedSessions.push(session)
            }
            displayedSessions.sort((first, second) => group.sessions.indexOf(first) - group.sessions.indexOf(second))
            const hiddenSessionCount = group.sessions.length - displayedSessions.length
            return (
              <section
                key={group.key}
                aria-label={`${groupName}，${group.sessions.length} 个终端`}
                className={cn(
                  'flex h-[33px] flex-none items-stretch overflow-hidden rounded-[7px] border border-[#26323c] bg-[#0f161d] hover:border-[#354451]',
                  groupVisible && 'border-[#344d42]',
                  groupFocused && 'border-[#315a48] bg-[#122019] shadow-[inset_0_-2px_var(--color-primary)]',
                )}
              >
                <button
                  type="button"
                  aria-expanded={groupExpanded}
                  aria-label={`${groupExpanded ? '收起' : '展开'} ${groupName} 终端组，${group.sessions.length} 个终端`}
                  onClick={() => setExpandedGroupKeys((current) => {
                    const next = new Set(current)
                    if (next.has(group.key)) next.delete(group.key)
                    else next.add(group.key)
                    return next
                  })}
                  title={device ? `${device.name} · ${device.hostname || device.id}` : '本机终端'}
                  className="flex max-w-[150px] min-w-[72px] cursor-pointer items-center gap-1.5 border-r border-[#26323c] px-2.5 hover:bg-[#17212a] max-md:max-w-[110px] max-md:min-w-[58px] max-md:px-2"
                >
                  {groupExpanded ? <ChevronDown className="size-2.5 flex-none text-[#71808b]" /> : <ChevronRight className="size-2.5 flex-none text-[#71808b]" />}
                  <i
                    data-running={group.sessions.some((session) => session.active)}
                    className="size-1.5 flex-none rounded-full bg-[#59636d] data-[running=true]:bg-primary data-[running=true]:shadow-[0_0_8px_#77f2b488]"
                  />
                  <strong className={cn('truncate text-[10px] font-semibold text-[#b9c5ce] max-md:text-[9px]', groupFocused && 'text-[#edf4f9]')}>
                    {groupName}
                  </strong>
                  <span className="flex-none font-mono text-[7px] text-[#61707b]">{group.sessions.length}</span>
                </button>

                {groupExpanded && <div className="flex flex-none items-stretch">
                  {displayedSessions.map((session) => {
                    const display = displayMap.get(session.id)
                    const placement = placements.get(session.id)
                    const focused = placement?.focused || false
                    const visible = placement?.visible || false
                    const onlineCount = session.services.filter((service) => service.status === 'online').length
                    return (
                      <span
                        key={session.id}
                        className={cn(
                          'flex flex-none items-stretch border-r border-[#222d36] bg-[#0d1319]',
                          visible && 'bg-[#13231c]',
                          focused && 'bg-[#193025] shadow-[inset_0_-2px_var(--color-primary)]',
                        )}
                      >
                        <button
                          ref={(node) => {
                            if (node) tabRefs.current.set(session.id, node)
                            else tabRefs.current.delete(session.id)
                          }}
                          type="button"
                          onClick={() => onSelect(session.id)}
                          aria-current={focused ? 'page' : undefined}
                          aria-label={`切换到 ${groupName} ${display?.label || session.name} ${display?.shortId || session.id.slice(0, 8)}`}
                          title={`${groupName} · ${display?.label || session.name} · ${session.id}${placement?.pane ? ` · 窗格 ${placement.pane}` : ''}`}
                          className={cn(
                            'flex max-w-[190px] min-w-0 cursor-pointer items-center gap-1 bg-transparent pr-2 pl-[3px] text-[#72808b] hover:bg-[#17212a] hover:text-[#c2ced6] max-md:max-w-[155px] max-md:pr-1.5',
                            visible && 'text-[#9acbb3]',
                            focused && 'text-primary hover:text-primary',
                          )}
                        >
                          <span className="relative my-auto flex-none">
                            <SessionSprite session={session} pose={(display?.ordinal || 1) === 1 ? 'sit' : 'stand'} />
                            {onlineCount > 0 && (
                              <i aria-label={`${onlineCount} 个开发服务运行中`} className="absolute -top-1 -right-1.5 grid min-w-3 place-items-center rounded-full bg-primary px-[3px] font-mono text-[7px] leading-[11px] font-bold not-italic text-[#07120d] shadow-[0_1px_4px_#000c]">
                                {onlineCount}
                              </i>
                            )}
                          </span>
                          <span className="min-w-0 truncate text-[9px] font-medium">{display?.label || session.name}</span>
                          <code className="flex-none font-mono text-[7px] text-[#63717c] max-md:hidden">{display?.shortId || session.id.slice(0, 8)}</code>
                          {placement?.pane && leaves.length > 1 && (
                            <span className={cn(
                              'flex-none rounded bg-[#1b252d] px-1 font-mono text-[7px] text-[#75838e]',
                              visible && 'bg-[#20382d] text-[#9ed1b7]',
                              focused && 'bg-primary text-[#07120d]',
                            )}>
                              {focused ? '当前' : visible ? '显示' : ''} P{placement.pane}
                            </span>
                          )}
                        </button>
                        <button
                          type="button"
                          aria-label={`关闭 ${groupName} ${display?.label || session.name}`}
                          onClick={() => onClose(session)}
                          className="grid w-5 flex-none cursor-pointer place-items-center bg-transparent text-[#586570] hover:bg-[#362027] hover:text-[#ff8d98] max-md:w-6"
                        >
                          <XIcon className="size-2.5" />
                        </button>
                      </span>
                    )
                  })}
                  {hiddenSessionCount > 0 && (
                    <button
                      type="button"
                      onClick={() => openSearch(groupName)}
                      aria-label={`查看 ${groupName} 其余 ${hiddenSessionCount} 个终端`}
                      className="flex w-9 flex-none cursor-pointer items-center justify-center border-r border-[#26323c] bg-[#10171e] font-mono text-[8px] text-[#82909b] hover:bg-[#17251f] hover:text-primary"
                    >
                      +{hiddenSessionCount}
                    </button>
                  )}
                  {group.deviceId && (
                      <button type="button" aria-label={`预览 ${groupName} 的开发服务`} title="预览设备本地开发服务" onClick={() => onPreview(cloneSource)} className="grid w-7 flex-none cursor-pointer place-items-center border-r border-[#26323c] bg-transparent text-[#709b88] hover:bg-[#193025] hover:text-primary">
                        <MonitorPlay className="size-3.5" />
                      </button>
                  )}
                  <button
                    type="button"
                    disabled={cloningId !== null}
                    aria-label={`在 ${groupName} 新建终端`}
                    title={group.deviceId ? '在同一设备新建终端' : '新建本地终端'}
                    onClick={() => onClone(cloneSource)}
                    className="grid w-7 flex-none cursor-pointer place-items-center bg-transparent text-[#709b88] hover:bg-[#193025] hover:text-primary disabled:cursor-wait disabled:text-[#536159] disabled:hover:bg-transparent"
                  >
                    {cloningId && group.sessions.some((session) => session.id === cloningId)
                      ? <LoaderCircle className="size-3 animate-spin" />
                      : <Plus className="size-3.5" />}
                  </button>
                </div>
                }
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

      <Dialog open={searchOpen} onOpenChange={(open) => { setSearchOpen(open); if (!open) setQuery('') }}>
        <DialogContent
          className="grid max-h-[min(720px,calc(100dvh-2rem))] grid-rows-[auto_auto_minmax(0,1fr)_auto] gap-0 overflow-hidden p-0 sm:max-w-[720px]"
          onCloseAutoFocus={(event) => {
            const sessionId = pendingFocusSessionRef.current
            if (!sessionId) return
            pendingFocusSessionRef.current = null
            // 避免 Radix 把焦点送回搜索按钮；新终端挂载后直接恢复键盘输入。
            event.preventDefault()
            window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
              const pane = document.querySelector(`[data-terminal-id="${sessionId}"]`)
              pane?.querySelector<HTMLElement>('.xterm-helper-textarea')?.focus({ preventScroll: true })
            }))
          }}
        >
          <DialogHeader className="border-b border-[#293641] px-5 pt-5 pb-4 pr-12">
            <DialogTitle className="text-base">查找终端</DialogTitle>
            <DialogDescription className="text-[10px]">搜索设备、终端、工作区、服务端口或运行状态。</DialogDescription>
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
            {!searchResults.length && <div className="px-4 py-12 text-center text-[11px] text-[#65747f]">没有匹配的终端</div>}
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
