import { useEffect, useMemo, useRef, useState } from 'react'

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
  onFocusLeaf: (leafId: string) => void
  onRatio: (splitId: string, ratio: number) => void
  onSplit: (session: TerminalSession, leafId: string, direction: LayoutDirection) => void
  onClosePane: (leafId: string) => void
  onPreviewService: (session: TerminalSession, service: DetectedService) => void
  onOpenWorkspace: (session: TerminalSession) => void
}

type Rect = { left: number; top: number; width: number; height: number }
type LeafPlacement = { leaf: LeafNode; rect: Rect }
type SashPlacement = { split: SplitNode; rect: Rect }

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
    onFocusLeaf,
    onRatio,
    onSplit,
    onClosePane,
    onPreviewService,
    onOpenWorkspace,
  } = props
  const rootRef = useRef<HTMLDivElement>(null)
  const { sessionPlacements, sashes, leafNumbers } = useMemo(() => {
    const placements = new Map<string, LeafPlacement>()
    const numbers = new Map<string, number>()
    const nextSashes: SashPlacement[] = []
    if (layout) {
      const leaves: LeafPlacement[] = []
      collectPlacements(layout, FULL_RECT, leaves, nextSashes)
      for (const [index, placement] of leaves.entries()) {
        numbers.set(placement.leaf.id, index + 1)
        for (const sessionId of placement.leaf.tabs) placements.set(sessionId, placement)
      }
    }
    return { sessionPlacements: placements, sashes: nextSashes, leafNumbers: numbers }
  }, [layout])
  const singleMode = forceSingle || !layout
  const terminalDisplays = useMemo(() => buildTerminalDisplayMap(sessions), [sessions])
  const visibleSessionIds = useMemo(() => {
    const ids = new Set<string>()
    if (!pageVisible) return ids
    if (singleMode) {
      if (activeId) ids.add(activeId)
      return ids
    }
    if (layout) {
      for (const leaf of listLeaves(layout)) {
        if (leaf.activeTab) ids.add(leaf.activeTab)
      }
    }
    return ids
  }, [activeId, layout, pageVisible, singleMode])
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
        const currentDevice = session.device_id
          ? devices.find((device) => device.id === session.device_id)
          : null
        const displaySession = currentDevice && currentDevice.name !== session.device_name
          ? { ...session, device_name: currentDevice.name }
          : session
        const placement = sessionPlacements.get(session.id)
        const rect = singleMode ? FULL_RECT : placement?.rect ?? FULL_RECT
        const visible = pageVisible && (singleMode
          ? session.id === activeId
          : placement?.leaf.activeTab === session.id)
        const leafId = placement?.leaf.id ?? null
        return (
          <div
            key={session.id}
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
            className={cn(
              'absolute min-h-0 min-w-0 overflow-hidden',
              !singleMode && visible && leafId === focusedLeafId && 'ring-2 ring-inset ring-[#4d9b74]',
            )}
          >
            <TerminalPane
              session={displaySession}
              terminalLabel={terminalDisplays.get(session.id)?.label || session.name || '终端'}
              workspaceLabel={session.workspace?.root || session.cwd || terminalDisplays.get(session.id)?.workspaceLabel || '默认目录'}
              paneNumber={!singleMode && leafId ? (leafNumbers.get(leafId) ?? null) : null}
              visible={visible}
              focused={visible && session.id === activeId}
              previewBusyPort={previewBusy?.terminalId === session.id ? previewBusy.port : null}
              canSplit={!singleMode && !splitting && Boolean(leafId)}
              canClosePane={!singleMode && sashes.length > 0 && Boolean(leafId)}
              onSplit={(direction) => { if (leafId) onSplit(session, leafId, direction) }}
              onClosePane={() => { if (leafId) onClosePane(leafId) }}
              onPreviewService={(service) => onPreviewService(session, service)}
              onOpenWorkspace={() => onOpenWorkspace(session)}
            />
          </div>
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
