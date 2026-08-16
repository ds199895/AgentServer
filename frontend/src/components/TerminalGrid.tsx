import { useEffect, useMemo, useRef, useState } from 'react'
import { Columns2, LoaderCircle, Plus, Rows2, Search, XIcon } from 'lucide-react'

import type { DetectedService, Device, TerminalSession } from '@/api'
import TerminalPane from '@/TerminalPane'
import {
  MAX_RATIO,
  MIN_RATIO,
  listLeaves,
  type LayoutDirection,
  type LayoutNode,
  type LeafNode,
  type SplitNode,
} from '@/terminal-layout'
import { buildTerminalDisplayMap } from '@/terminal-display'
import { cn } from '@/lib/utils'

type Props = {
  layout: LayoutNode | null
  devices: Device[]
  sessions: TerminalSession[]
  /** page === 'terminals' — 离开终端页时所有 pane 隐藏但保持挂载 */
  pageVisible: boolean
  /** 单窗格模式下唯一可见的会话 */
  activeId: string | null
  focusedLeafId: string | null
  /** 移动端忽略分屏几何，但不改动已保存的桌面布局 */
  forceSingle: boolean
  previewBusy: { terminalId: string; port: number } | null
  splitting: boolean
  creatingTab: boolean
  onFocusLeaf: (leafId: string) => void
  /** 激活指定 leaf 内的标签，并把全局焦点同步到该 leaf。 */
  onActivateTab: (
    sessionId: string,
    leafId: string,
    options: { focusTerminal: boolean },
  ) => void
  /** 在指定 leaf 内按当前标签的设备/工作区 profile 新建标签。 */
  onNewTab: (source: TerminalSession, leafId: string) => void
  /** 为指定 leaf 打开终端查找器；空 leaf 可借此选择已有会话。 */
  onFindTab: (leafId: string) => void
  /** 关闭终端后台会话；调用侧负责确认和布局收缩。 */
  onCloseTerminal: (session: TerminalSession) => void
  onRatio: (splitId: string, ratio: number) => void
  onSplit: (session: TerminalSession, leafId: string, direction: LayoutDirection) => void
  onClosePane: (leafId: string) => void
  onPreviewService: (session: TerminalSession, service: DetectedService) => void
  onOpenWorkspace: (session: TerminalSession) => void
}

type Rect = { left: number; top: number; width: number; height: number }
type LeafPlacement = { leaf: LeafNode; rect: Rect }
type SashPlacement = { split: SplitNode; rect: Rect }
type TerminalPaneTab = {
  session: TerminalSession
  terminalLabel: string
  workspaceLabel: string
}

const FULL_RECT: Rect = { left: 0, top: 0, width: 1, height: 1 }
const MAX_CACHED_TERMINALS = 8

function collectPlacements(
  node: LayoutNode,
  rect: Rect,
  leaves: LeafPlacement[],
  sashes: SashPlacement[],
): void {
  if (node.type === 'leaf') {
    leaves.push({ leaf: node, rect })
    return
  }
  sashes.push({ split: node, rect })
  if (node.direction === 'row') {
    const firstWidth = rect.width * node.ratio
    collectPlacements(node.children[0], { ...rect, width: firstWidth }, leaves, sashes)
    collectPlacements(
      node.children[1],
      { ...rect, left: rect.left + firstWidth, width: rect.width - firstWidth },
      leaves,
      sashes,
    )
    return
  }
  const firstHeight = rect.height * node.ratio
  collectPlacements(node.children[0], { ...rect, height: firstHeight }, leaves, sashes)
  collectPlacements(
    node.children[1],
    { ...rect, top: rect.top + firstHeight, height: rect.height - firstHeight },
    leaves,
    sashes,
  )
}

function percent(value: number): string {
  return `${value * 100}%`
}

/**
 * 分屏分隔条只操作布局几何。终端组件在另一个稳定的扁平层中挂载，
 * 因此拆分、移动和桌面/移动端切换都不会重建 xterm 或 WebSocket。
 */
function SplitSash({
  placement,
  rootRef,
  onRatio,
}: {
  placement: SashPlacement
  rootRef: React.RefObject<HTMLDivElement | null>
  onRatio: (splitId: string, ratio: number) => void
}) {
  const { split, rect } = placement
  const horizontal = split.direction === 'row'
  const dragRef = useRef<{
    pointerId: number
    startPosition: number
    startRatio: number
    size: number
  } | null>(null)
  const frameRef = useRef<number | null>(null)
  const pendingRatioRef = useRef<number | null>(null)
  const boundary = horizontal
    ? rect.left + rect.width * split.ratio
    : rect.top + rect.height * split.ratio

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.stopPropagation()
    const rootRect = rootRef.current?.getBoundingClientRect()
    if (!rootRect) return
    const size = horizontal ? rootRect.width * rect.width : rootRect.height * rect.height
    if (size <= 0) return
    dragRef.current = {
      pointerId: event.pointerId,
      startPosition: horizontal ? event.clientX : event.clientY,
      startRatio: split.ratio,
      size,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const position = horizontal ? event.clientX : event.clientY
    pendingRatioRef.current = drag.startRatio + (position - drag.startPosition) / drag.size
    if (frameRef.current === null) {
      frameRef.current = window.requestAnimationFrame(() => {
        frameRef.current = null
        const ratio = pendingRatioRef.current
        pendingRatioRef.current = null
        if (ratio !== null) onRatio(split.id, ratio)
      })
    }
  }
  const finishDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current)
    frameRef.current = null
    const ratio = pendingRatioRef.current
    pendingRatioRef.current = null
    if (ratio !== null) onRatio(split.id, ratio)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const decrease = horizontal ? event.key === 'ArrowLeft' : event.key === 'ArrowUp'
    const increase = horizontal ? event.key === 'ArrowRight' : event.key === 'ArrowDown'
    let ratio: number | null = null
    if (decrease) ratio = split.ratio - 0.02
    if (increase) ratio = split.ratio + 0.02
    if (event.key === 'Home') ratio = MIN_RATIO
    if (event.key === 'End') ratio = MAX_RATIO
    if (ratio === null) return
    event.preventDefault()
    event.stopPropagation()
    onRatio(split.id, ratio)
  }

  useEffect(() => () => {
    if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current)
  }, [])

  return (
    <div
      role="separator"
      tabIndex={0}
      aria-orientation={horizontal ? 'vertical' : 'horizontal'}
      aria-valuemin={Math.round(MIN_RATIO * 100)}
      aria-valuemax={Math.round(MAX_RATIO * 100)}
      aria-valuenow={Math.round(split.ratio * 100)}
      aria-label={horizontal ? '调整左右终端宽度' : '调整上下终端高度'}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finishDrag}
      onPointerCancel={finishDrag}
      onKeyDown={onKeyDown}
      style={horizontal
        ? {
            left: `calc(${percent(boundary)} - 4px)`,
            top: percent(rect.top),
            width: '9px',
            height: percent(rect.height),
          }
        : {
            left: percent(rect.left),
            top: `calc(${percent(boundary)} - 4px)`,
            width: percent(rect.width),
            height: '9px',
          }}
      className={cn(
        'group absolute z-30 touch-none focus-visible:outline-1 focus-visible:outline-primary',
        horizontal ? 'cursor-col-resize' : 'cursor-row-resize',
      )}
    >
      <div
        className={cn(
          'bg-[#1b2630] transition-colors group-hover:bg-[#315a48] group-active:bg-primary',
          horizontal ? 'mx-auto h-full w-px' : 'my-auto h-px w-full',
        )}
      />
    </div>
  )
}

function PaneTabStrip({
  paneNumber,
  tabs,
  activeTabId,
  focused,
  canSplit,
  canClosePane,
  creatingTab,
  onActivateTab,
  onCloseTerminal,
  onNewTab,
  onFindTab,
  onSplit,
  onClosePane,
}: {
  paneNumber: number
  tabs: TerminalPaneTab[]
  activeTabId: string | null
  focused: boolean
  canSplit: boolean
  canClosePane: boolean
  creatingTab: boolean
  onActivateTab: (sessionId: string, options: { focusTerminal: boolean }) => void
  onCloseTerminal: (session: TerminalSession) => void
  onNewTab: () => void
  onFindTab: () => void
  onSplit: (direction: LayoutDirection) => void
  onClosePane: () => void
}) {
  const tabRefs = useRef(new Map<string, HTMLButtonElement>())

  useEffect(() => {
    if (!activeTabId) return
    const frame = window.requestAnimationFrame(() => {
      tabRefs.current.get(activeTabId)?.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
        inline: 'nearest',
      })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [activeTabId])

  const activateByKeyboard = (currentIndex: number, key: string) => {
    if (!tabs.length) return
    let nextIndex: number | null = null
    if (key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
    if (key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length
    if (key === 'Home') nextIndex = 0
    if (key === 'End') nextIndex = tabs.length - 1
    if (nextIndex === null) return
    const targetId = tabs[nextIndex].session.id
    onActivateTab(targetId, { focusTerminal: false })
    window.requestAnimationFrame(() => {
      tabRefs.current.get(targetId)?.focus({ preventScroll: true })
    })
  }

  return (
    <header
      aria-label={`窗格 ${paneNumber} 终端栏`}
      className={cn(
        'pointer-events-auto flex h-[34px] min-w-0 items-stretch border-b border-[#26313a] bg-[#0a1015] max-md:h-8',
        focused && 'border-[#315a48] bg-[#0d1713]',
      )}
    >
      <span
        aria-label={`窗格 ${paneNumber}`}
        className="grid min-w-9 flex-none place-items-center border-r border-[#26313a] bg-[#10171d] font-mono text-[8px] font-semibold text-[#8d9ba5]"
      >
        P{paneNumber}
      </span>
      <div
        role="tablist"
        aria-orientation="horizontal"
        aria-label={`窗格 ${paneNumber} 终端标签`}
        data-terminal-pane-tabs={paneNumber}
        className="flex min-w-0 flex-1 items-stretch overflow-x-auto overflow-y-hidden [scrollbar-color:#34404c_transparent] [scrollbar-width:thin]"
      >
        {tabs.map((tab, index) => {
          const active = tab.session.id === activeTabId
          const tabDeviceLabel = tab.session.device_name || '本机'
          const runtimeLabel = tab.session.active ? '运行中' : '已退出'
          return (
            <span
              key={tab.session.id}
              role="presentation"
              className={cn(
                'flex min-w-[148px] max-w-[260px] flex-none items-stretch border-r border-[#25303a] bg-[#0c1218] text-[#71808b]',
                active && 'bg-[#15231d] text-[#dce8e1] shadow-[inset_0_-2px_var(--color-primary)]',
              )}
            >
              <button
                ref={(node) => {
                  if (node) tabRefs.current.set(tab.session.id, node)
                  else tabRefs.current.delete(tab.session.id)
                }}
                type="button"
                role="tab"
                id={`terminal-tab-${tab.session.id}`}
                data-terminal-tab-id={tab.session.id}
                aria-selected={active}
                aria-controls={`terminal-panel-${tab.session.id}`}
                tabIndex={active || (!activeTabId && index === 0) ? 0 : -1}
                aria-label={`窗格 ${paneNumber} 激活 ${tabDeviceLabel} ${tab.terminalLabel} ${tab.session.id.slice(0, 8)} ${runtimeLabel}`}
                title={`${tabDeviceLabel} · ${tab.terminalLabel} · ${tab.workspaceLabel} · ${tab.session.id} · ${runtimeLabel}`}
                onClick={() => onActivateTab(tab.session.id, { focusTerminal: true })}
                onKeyDown={(event) => {
                  if (event.key === 'Delete') {
                    event.preventDefault()
                    event.stopPropagation()
                    onCloseTerminal(tab.session)
                    return
                  }
                  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
                  event.preventDefault()
                  event.stopPropagation()
                  activateByKeyboard(index, event.key)
                }}
                className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 bg-transparent px-2 text-left hover:bg-[#17212a] hover:text-[#d2dde4] focus-visible:outline-1 focus-visible:outline-primary"
              >
                <i
                  aria-hidden="true"
                  className={cn(
                    'size-1.5 flex-none rounded-full bg-[#7b555d]',
                    tab.session.active && 'bg-primary shadow-[0_0_7px_#77f2b477]',
                  )}
                />
                <span className="min-w-0 truncate text-[9px] font-semibold">
                  {tabDeviceLabel} / {tab.terminalLabel}
                </span>
                <code className="flex-none font-mono text-[7px] text-[#60717d]">
                  {tab.session.id.slice(0, 6)}
                </code>
              </button>
              <button
                type="button"
                aria-label={`关闭窗格 ${paneNumber} 的终端 ${tabDeviceLabel} ${tab.terminalLabel}`}
                title={`关闭终端并结束后台会话 · ${tab.session.id.slice(0, 8)}`}
                onClick={() => onCloseTerminal(tab.session)}
                className="grid w-6 flex-none cursor-pointer place-items-center bg-transparent text-[#52616c] hover:bg-[#352027] hover:text-[#ff929d] focus-visible:outline-1 focus-visible:outline-[#ff929d]"
              >
                <XIcon className="size-2.5" />
              </button>
            </span>
          )
        })}
        {!tabs.length && (
          <span aria-hidden="true" className="flex min-w-24 flex-1 items-center px-3 font-mono text-[8px] text-[#5e6d78]">
            空窗格
          </span>
        )}
      </div>
      <div className="flex flex-none items-center gap-0.5 border-l border-[#26313a] bg-[#0f161c] px-1">
        {!tabs.length ? (
          <button
            type="button"
            onClick={onFindTab}
            aria-label={`向窗格 ${paneNumber} 添加终端`}
            title="选择未归属终端；没有可选会话时前往设备页新建"
            className="flex h-6 cursor-pointer items-center gap-1 rounded px-1.5 text-[8px] font-semibold text-[#82918c] hover:bg-[#193025] hover:text-primary"
          >
            <Search className="size-3.5" />
            <span className="max-lg:hidden">添加终端</span>
          </button>
        ) : (
          <button
            type="button"
            onClick={onNewTab}
            disabled={creatingTab}
            aria-label={`在窗格 ${paneNumber} 新建终端标签`}
            title="在当前窗格新建同设备、同工作区终端"
            className="grid size-6 cursor-pointer place-items-center rounded text-[#82918c] hover:bg-[#193025] hover:text-primary disabled:cursor-wait disabled:text-[#536159] disabled:hover:bg-transparent"
          >
            {creatingTab ? <LoaderCircle className="size-3 animate-spin" /> : <Plus className="size-3.5" />}
          </button>
        )}
        {canSplit && (
          <>
            <button
              type="button"
              onClick={() => onSplit('row')}
              aria-label={`从窗格 ${paneNumber} 新建同设备终端并向右分屏`}
              title="新建同设备终端并向右分屏"
              className="grid size-6 cursor-pointer place-items-center rounded text-[#82918c] hover:bg-[#193025] hover:text-primary"
            >
              <Columns2 className="size-3.5" />
            </button>
            <button
              type="button"
              onClick={() => onSplit('column')}
              aria-label={`从窗格 ${paneNumber} 新建同设备终端并向下分屏`}
              title="新建同设备终端并向下分屏"
              className="grid size-6 cursor-pointer place-items-center rounded text-[#82918c] hover:bg-[#193025] hover:text-primary"
            >
              <Rows2 className="size-3.5" />
            </button>
          </>
        )}
        {canClosePane && (
          <button
            type="button"
            onClick={onClosePane}
            aria-label={`关闭窗格 ${paneNumber}（保留终端）`}
            title="关闭窗格（保留终端）"
            className="grid size-6 cursor-pointer place-items-center rounded text-[#75838e] hover:bg-[#2b1b20] hover:text-[#ff9aa4]"
          >
            <XIcon className="size-3" />
          </button>
        )}
      </div>
    </header>
  )
}

/**
 * 可见终端和最近使用的少量终端保持稳定挂载，因此日常切换仍保留
 * xterm、选区和滚动位置。超出热缓存的隐藏会话会卸载 xterm/WS，
 * 重新打开时依靠服务端快照恢复，避免数十个会话同时消耗浏览器资源。
 */
export function TerminalGrid(props: Props) {
  const {
    layout,
    devices,
    sessions,
    pageVisible,
    activeId,
    focusedLeafId,
    forceSingle,
    previewBusy,
    splitting,
    creatingTab,
    onFocusLeaf,
    onActivateTab,
    onNewTab,
    onFindTab,
    onCloseTerminal,
    onRatio,
    onSplit,
    onClosePane,
    onPreviewService,
    onOpenWorkspace,
  } = props
  const rootRef = useRef<HTMLDivElement>(null)
  const { leafPlacements, sessionPlacements, sashes, leafNumbers } = useMemo(() => {
    const placements = new Map<string, LeafPlacement>()
    const numbers = new Map<string, number>()
    const leaves: LeafPlacement[] = []
    const nextSashes: SashPlacement[] = []
    if (layout) {
      collectPlacements(layout, FULL_RECT, leaves, nextSashes)
      for (const [index, placement] of leaves.entries()) {
        numbers.set(placement.leaf.id, index + 1)
        for (const sessionId of placement.leaf.tabs) placements.set(sessionId, placement)
      }
    }
    return {
      leafPlacements: leaves,
      sessionPlacements: placements,
      sashes: nextSashes,
      leafNumbers: numbers,
    }
  }, [layout])
  const singleMode = forceSingle || !layout
  const terminalDisplays = useMemo(() => buildTerminalDisplayMap(sessions), [sessions])
  const deviceMap = useMemo(
    () => new Map(devices.map((device) => [device.id, device])),
    [devices],
  )
  const displaySessions = useMemo(() => new Map(sessions.map((session) => {
    const currentDevice = session.device_id ? deviceMap.get(session.device_id) : null
    return [
      session.id,
      currentDevice && currentDevice.name !== session.device_name
        ? { ...session, device_name: currentDevice.name }
        : session,
    ] as const
  })), [deviceMap, sessions])
  const paneTabsByLeafId = useMemo(() => {
    const result = new Map<string, TerminalPaneTab[]>()
    if (!layout) return result
    for (const leaf of listLeaves(layout)) {
      const tabs = leaf.tabs.flatMap((sessionId) => {
        const tabSession = displaySessions.get(sessionId)
        if (!tabSession) return []
        const display = terminalDisplays.get(sessionId)
        return [{
          session: tabSession,
          terminalLabel: display?.label || tabSession.name || '终端',
          workspaceLabel: tabSession.workspace?.root || tabSession.cwd || display?.workspaceLabel || '默认目录',
        }]
      })
      result.set(leaf.id, tabs)
    }
    return result
  }, [displaySessions, layout, terminalDisplays])
  const singleLeafPlacement = useMemo(() => {
    if (!singleMode || !leafPlacements.length) return null
    return leafPlacements.find((placement) => placement.leaf.id === focusedLeafId)
      || (activeId ? sessionPlacements.get(activeId) : null)
      || leafPlacements[0]
  }, [activeId, focusedLeafId, leafPlacements, sessionPlacements, singleMode])
  const paneFramePlacements = singleMode
    ? (singleLeafPlacement ? [{ leaf: singleLeafPlacement.leaf, rect: FULL_RECT }] : [])
    : leafPlacements
  const singleVisibleSessionId = singleMode
    ? singleLeafPlacement?.leaf.activeTab ?? (!layout ? activeId : null)
    : null
  const visibleSessionIds = useMemo(() => {
    const ids = new Set<string>()
    if (!pageVisible) return ids
    if (singleMode) {
      if (singleVisibleSessionId) ids.add(singleVisibleSessionId)
      return ids
    }
    if (layout) {
      for (const leaf of listLeaves(layout)) {
        if (leaf.activeTab) ids.add(leaf.activeTab)
      }
    }
    return ids
  }, [layout, pageVisible, singleMode, singleVisibleSessionId])
  const visibleKey = [...visibleSessionIds].join('\u0000')
  const [cachedSessionIds, setCachedSessionIds] = useState<string[]>([])

  useEffect(() => {
    const validIds = new Set(sessions.map((session) => session.id))
    const requiredIds = new Set(visibleSessionIds)
    setCachedSessionIds((current) => {
      const next = current.filter((id) => validIds.has(id) && !requiredIds.has(id))
      for (const id of visibleSessionIds) next.push(id)
      while (next.length > MAX_CACHED_TERMINALS) {
        const removable = next.findIndex((id) => !requiredIds.has(id))
        if (removable < 0) break
        next.splice(removable, 1)
      }
      return next.length === current.length && next.every((id, index) => id === current[index])
        ? current
        : next
    })
  }, [sessions, visibleKey])

  // The effect below maintains recency, but effects run after React commits.
  // Trim the derived set as well so opening a cold terminal never creates a
  // throwaway ninth xterm/WebSocket for one render while the cache is full.
  const hiddenCacheBudget = Math.max(0, MAX_CACHED_TERMINALS - visibleSessionIds.size)
  const hiddenCachedIds = hiddenCacheBudget > 0
    ? cachedSessionIds
        .filter((id) => !visibleSessionIds.has(id))
        .slice(-hiddenCacheBudget)
    : []
  const mountedIds = new Set([
    ...hiddenCachedIds,
    ...visibleSessionIds,
  ])
  const mountedSessions = sessions.filter((session) => mountedIds.has(session.id))

  return (
    <div
      ref={rootRef}
      data-mounted-terminal-count={mountedSessions.length}
      className="relative h-full w-full min-h-0 min-w-0 overflow-hidden"
    >
      {mountedSessions.map((session) => {
        const displaySession = displaySessions.get(session.id) || session
        const terminalDisplay = terminalDisplays.get(session.id)
        const placement = sessionPlacements.get(session.id)
        const rect = singleMode ? FULL_RECT : placement?.rect ?? FULL_RECT
        const visible = pageVisible && (singleMode
          ? session.id === singleVisibleSessionId
          : placement?.leaf.activeTab === session.id)
        const leafId = placement?.leaf.id ?? null
        return (
          <div
            key={session.id}
            id={leafId ? `terminal-panel-${session.id}` : undefined}
            role={leafId ? 'tabpanel' : undefined}
            aria-labelledby={leafId ? `terminal-tab-${session.id}` : undefined}
            aria-hidden={!visible}
            data-terminal-id={session.id}
            data-terminal-leaf-id={leafId ?? undefined}
            data-terminal-visible={visible ? 'true' : 'false'}
            data-terminal-focused={visible && session.id === activeId ? 'true' : 'false'}
            onPointerDownCapture={() => { if (visible && leafId) onFocusLeaf(leafId) }}
            onFocusCapture={() => { if (visible && leafId) onFocusLeaf(leafId) }}
            style={{
              left: percent(rect.left),
              top: percent(rect.top),
              width: percent(rect.width),
              height: percent(rect.height),
              pointerEvents: visible ? 'auto' : 'none',
            }}
            className="absolute min-h-0 min-w-0 overflow-hidden"
          >
            <div className="absolute inset-x-0 top-[34px] bottom-0 min-h-0 min-w-0 max-md:top-8">
              <TerminalPane
                session={displaySession}
                terminalLabel={terminalDisplay?.label || session.name || '终端'}
                workspaceLabel={session.workspace?.root || session.cwd || terminalDisplay?.workspaceLabel || '默认目录'}
                paneNumber={leafId ? (leafNumbers.get(leafId) ?? null) : null}
                visible={visible}
                focused={visible && session.id === activeId}
                previewBusyPort={previewBusy?.terminalId === session.id ? previewBusy.port : null}
                onPreviewService={(service) => onPreviewService(displaySession, service)}
                onOpenWorkspace={() => onOpenWorkspace(displaySession)}
              />
            </div>
          </div>
        )
      })}
      {pageVisible && paneFramePlacements.map((placement, index) => {
        const { leaf, rect } = placement
        const paneNumber = leafNumbers.get(leaf.id) ?? index + 1
        const tabs = paneTabsByLeafId.get(leaf.id) || []
        const activeTab = tabs.find((tab) => tab.session.id === leaf.activeTab) || tabs[0] || null
        const focused = leaf.id === focusedLeafId || (singleMode && singleLeafPlacement?.leaf.id === leaf.id)
        return (
          <section
            key={leaf.id}
            data-terminal-pane-root="true"
            data-terminal-leaf-id={leaf.id}
            data-terminal-pane-number={paneNumber}
            data-terminal-pane-empty={tabs.length === 0 ? 'true' : 'false'}
            aria-label={`终端窗格 ${paneNumber}`}
            style={{
              left: percent(rect.left),
              top: percent(rect.top),
              width: percent(rect.width),
              height: percent(rect.height),
            }}
            className={cn(
              'pointer-events-none absolute z-20 min-h-0 min-w-0 overflow-hidden',
              focused && 'ring-2 ring-inset ring-[#4d9b74]',
            )}
          >
            <PaneTabStrip
              paneNumber={paneNumber}
              tabs={tabs}
              activeTabId={activeTab?.session.id ?? null}
              focused={focused}
              canSplit={!singleMode && !splitting && Boolean(activeTab)}
              canClosePane={!singleMode && sashes.length > 0}
              creatingTab={creatingTab}
              onActivateTab={(sessionId, options) => onActivateTab(sessionId, leaf.id, options)}
              onCloseTerminal={onCloseTerminal}
              onNewTab={() => { if (activeTab) onNewTab(activeTab.session, leaf.id) }}
              onFindTab={() => {
                onFocusLeaf(leaf.id)
                onFindTab(leaf.id)
              }}
              onSplit={(direction) => { if (activeTab) onSplit(activeTab.session, leaf.id, direction) }}
              onClosePane={() => onClosePane(leaf.id)}
            />
            {!tabs.length && (
              <div
                role="region"
                aria-label={`空终端窗格 ${paneNumber}`}
                tabIndex={0}
                onPointerDown={() => onFocusLeaf(leaf.id)}
                onFocus={() => onFocusLeaf(leaf.id)}
                className="pointer-events-auto absolute inset-x-0 top-[34px] bottom-0 grid place-items-center bg-[#0b0f14] px-5 text-center focus-visible:outline-1 focus-visible:outline-primary max-md:top-8"
              >
                <div role="status">
                  <strong className="block text-[11px] font-semibold text-[#9ba8b1]">窗格 {paneNumber} 暂无终端</strong>
                  <span className="mt-1.5 block max-w-72 text-[9px] leading-4 text-[#5f6d77]">
                    添加尚未归属的会话，或前往设备页新建终端。
                  </span>
                </div>
              </div>
            )}
          </section>
        )
      })}
      {!singleMode && pageVisible && sashes.map((placement) => (
        <SplitSash
          key={placement.split.id}
          placement={placement}
          rootRef={rootRef}
          onRatio={onRatio}
        />
      ))}
    </div>
  )
}
