import { useEffect, useMemo, useRef } from 'react'

import type { DetectedService, TerminalSession } from '@/api'
import TerminalPane from '@/TerminalPane'
import {
  MAX_RATIO,
  MIN_RATIO,
  type LayoutDirection,
  type LayoutNode,
  type LeafNode,
  type SplitNode,
} from '@/terminal-layout'
import { cn } from '@/lib/utils'

type Props = {
  layout: LayoutNode | null
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
  onPreviewService: (session: TerminalSession, service: DetectedService) => void
  onOpenWorkspace: (session: TerminalSession) => void
}

type Rect = { left: number; top: number; width: number; height: number }
type LeafPlacement = { leaf: LeafNode; rect: Rect }
type SashPlacement = { split: SplitNode; rect: Rect }

const FULL_RECT: Rect = { left: 0, top: 0, width: 1, height: 1 }

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
 * 终端始终由同一个 sessions.map 扁平渲染。布局树只决定 wrapper 的几何和
 * 可见性；无论 leaf 被拆分、会话跨 leaf 移动还是响应式切换，React 的
 * parent + key 都保持不变，因此 xterm、WebSocket、选区和滚动位置都保留。
 */
export function TerminalGrid(props: Props) {
  const {
    layout,
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
    onPreviewService,
    onOpenWorkspace,
  } = props
  const rootRef = useRef<HTMLDivElement>(null)
  const { sessionPlacements, sashes } = useMemo(() => {
    const placements = new Map<string, LeafPlacement>()
    const nextSashes: SashPlacement[] = []
    if (layout) {
      const leaves: LeafPlacement[] = []
      collectPlacements(layout, FULL_RECT, leaves, nextSashes)
      for (const placement of leaves) {
        for (const sessionId of placement.leaf.tabs) placements.set(sessionId, placement)
      }
    }
    return { sessionPlacements: placements, sashes: nextSashes }
  }, [layout])
  const singleMode = forceSingle || !layout

  return (
    <div ref={rootRef} className="relative h-full w-full min-h-0 min-w-0 overflow-hidden">
      {sessions.map((session) => {
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
              !singleMode && visible && leafId === focusedLeafId && 'ring-1 ring-inset ring-[#3d7a5c]',
            )}
          >
            <TerminalPane
              session={session}
              visible={visible}
              focused={visible && session.id === activeId}
              previewBusyPort={previewBusy?.terminalId === session.id ? previewBusy.port : null}
              canSplit={!singleMode && !splitting && Boolean(leafId)}
              onSplit={(direction) => { if (leafId) onSplit(session, leafId, direction) }}
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
