import { useEffect, useMemo, useRef, useState } from 'react'
import { Columns2, LoaderCircle, Pin, Plus, Rows2, XIcon } from 'lucide-react'

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
  creatingTab: boolean
  onFocusLeaf: (leafId: string) => void
  /** 激活指定 leaf 内的标签，并把全局焦点同步到该 leaf。 */
  onActivateTab: (
    sessionId: string,
    leafId: string,
    options: { focusTerminal: boolean },
  ) => void
  /** 把指定窗格中的临时预览标签固定为普通标签。 */
  onPinTab: (sessionId: string, leafId: string) => void
  /** 仅解除指定窗格中的标签归属；后台终端继续运行。 */
  onDetachTab: (sessionId: string, leafId: string) => void
  /** 拖动标签到另一个窗格或在当前窗格内排序；不触碰后台终端。 */
  onMoveTab: (
    sessionId: string,
    sourceLeafId: string,
    targetLeafId: string,
    targetIndex: number,
  ) => void
  /** 在指定 leaf 内按当前标签的设备/工作区 profile 新建标签。 */
  onNewTab: (source: TerminalSession, leafId: string) => void
  onRatio: (splitId: string, ratio: number) => void
  /** 纯布局分屏；新窗格默认为空，不创建或复制后台终端。 */
  onSplit: (leafId: string, direction: LayoutDirection) => void
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
  preview: boolean
}
type DraggedPaneTab = { sessionId: string; sourceLeafId: string }
type PaneDropTarget = { leafId: string; index: number }

const FULL_RECT: Rect = { left: 0, top: 0, width: 1, height: 1 }
const MAX_CACHED_TERMINALS = 8
const TERMINAL_TAB_DRAG_TYPE = 'application/x-agentserver-terminal-tab'

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
  leafId,
  paneNumber,
  tabs,
  activeTabId,
  focused,
  draggedTab,
  dropTarget,
  canSplit,
  canClosePane,
  creatingTab,
  onActivateTab,
  onPinTab,
  onDetachTab,
  onTabDragStart,
  onTabDragOver,
  onTabDrop,
  onTabDragEnd,
  onNewTab,
  onSplit,
  onClosePane,
}: {
  leafId: string
  paneNumber: number
  tabs: TerminalPaneTab[]
  activeTabId: string | null
  focused: boolean
  draggedTab: DraggedPaneTab | null
  dropTarget: PaneDropTarget | null
  canSplit: boolean
  canClosePane: boolean
  creatingTab: boolean
  onActivateTab: (sessionId: string, options: { focusTerminal: boolean }) => void
  onPinTab: (sessionId: string) => void
  onDetachTab: (sessionId: string) => void
  onTabDragStart: (event: React.DragEvent<HTMLButtonElement>, sessionId: string) => void
  onTabDragOver: (event: React.DragEvent<HTMLElement>, targetIndex: number) => void
  onTabDrop: (event: React.DragEvent<HTMLElement>, targetIndex: number) => void
  onTabDragEnd: () => void
  onNewTab: () => void
  onSplit: (direction: LayoutDirection) => void
  onClosePane: () => void
}) {
  const tabRefs = useRef(new Map<string, HTMLButtonElement>())
  const activePreviewTab = tabs.find((tab) => (
    tab.preview && tab.session.id === activeTabId
  ))

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

  const dropIndexForTab = (event: React.DragEvent<HTMLElement>, index: number) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return event.clientX < rect.left + rect.width / 2 ? index : index + 1
  }

  const scrollTabListNearEdge = (event: React.DragEvent<HTMLElement>) => {
    const tabList = event.currentTarget.closest<HTMLElement>('[role="tablist"]')
    if (!tabList) return
    const rect = tabList.getBoundingClientRect()
    if (event.clientX < rect.left + 32) tabList.scrollLeft -= 14
    else if (event.clientX > rect.right - 32) tabList.scrollLeft += 14
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
        data-terminal-drop-leaf-id={leafId}
        className="flex min-w-0 flex-1 items-stretch overflow-x-auto overflow-y-hidden [scrollbar-color:#34404c_transparent] [scrollbar-width:thin]"
      >
        {tabs.map((tab, index) => {
          const active = tab.session.id === activeTabId
          const tabDeviceLabel = tab.session.device_name || '本机'
          const runtimeLabel = tab.session.active ? '运行中' : '已退出'
          const tabModeLabel = tab.preview ? '临时预览，双击固定' : '已固定'
          const dropBefore = dropTarget?.leafId === leafId && dropTarget.index === index
          const dropAfter = (
            index === tabs.length - 1 &&
            dropTarget?.leafId === leafId &&
            dropTarget.index === tabs.length
          )
          return (
            <span
              key={tab.session.id}
              role="presentation"
              data-terminal-tab-mode={tab.preview ? 'preview' : 'pinned'}
              data-terminal-drag-wrapper={tab.session.id}
              data-terminal-drop-leaf-id={leafId}
              data-terminal-drop-index={dropBefore ? index : dropAfter ? index + 1 : undefined}
              data-terminal-drop-active={dropBefore || dropAfter ? 'true' : 'false'}
              onDragOver={(event) => {
                if (!draggedTab) return
                event.preventDefault()
                event.stopPropagation()
                event.dataTransfer.dropEffect = 'move'
                scrollTabListNearEdge(event)
                onTabDragOver(event, dropIndexForTab(event, index))
              }}
              onDrop={(event) => {
                if (!draggedTab) return
                event.preventDefault()
                event.stopPropagation()
                onTabDrop(event, dropIndexForTab(event, index))
              }}
              className={cn(
                'relative flex min-w-[148px] max-w-[260px] flex-none items-stretch border-r border-[#25303a] bg-[#0c1218] text-[#71808b]',
                active && 'bg-[#15231d] text-[#dce8e1] shadow-[inset_0_-2px_var(--color-primary)]',
                tab.preview && 'bg-[#101820] text-[#94a2ad]',
                active && tab.preview && 'bg-[#15231d] text-[#dce8e1]',
                draggedTab?.sessionId === tab.session.id && 'opacity-45',
              )}
            >
              {dropBefore && (
                <i aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-0 z-10 w-0.5 bg-primary shadow-[0_0_8px_#77f2b4]" />
              )}
              {dropAfter && (
                <i aria-hidden="true" className="pointer-events-none absolute inset-y-0 right-0 z-10 w-0.5 bg-primary shadow-[0_0_8px_#77f2b4]" />
              )}
              <button
                ref={(node) => {
                  if (node) tabRefs.current.set(tab.session.id, node)
                  else tabRefs.current.delete(tab.session.id)
                }}
                type="button"
                role="tab"
                id={`terminal-tab-${tab.session.id}`}
                data-terminal-tab-id={tab.session.id}
                data-terminal-drag-id={tab.session.id}
                data-terminal-tab-mode={tab.preview ? 'preview' : 'pinned'}
                draggable
                aria-selected={active}
                aria-controls={`terminal-panel-${tab.session.id}`}
                tabIndex={active || (!activeTabId && index === 0) ? 0 : -1}
                aria-label={`窗格 ${paneNumber} 激活 ${tabDeviceLabel} ${tab.terminalLabel} ${tab.session.id.slice(0, 8)} ${runtimeLabel} ${tabModeLabel}`}
                title={`${tabDeviceLabel} · ${tab.terminalLabel} · ${tab.workspaceLabel} · ${tab.session.id} · ${runtimeLabel} · ${tabModeLabel}`}
                onDragStart={(event) => onTabDragStart(event, tab.session.id)}
                onDragEnd={onTabDragEnd}
                onClick={() => onActivateTab(tab.session.id, { focusTerminal: true })}
                onDoubleClick={() => { if (tab.preview) onPinTab(tab.session.id) }}
                onKeyDown={(event) => {
                  if (event.key === 'Delete') {
                    event.preventDefault()
                    event.stopPropagation()
                    onDetachTab(tab.session.id)
                    return
                  }
                  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
                  event.preventDefault()
                  event.stopPropagation()
                  activateByKeyboard(index, event.key)
                }}
                className="flex min-w-0 flex-1 cursor-grab select-none items-center gap-1.5 bg-transparent px-2 text-left hover:bg-[#17212a] hover:text-[#d2dde4] active:cursor-grabbing focus-visible:outline-1 focus-visible:outline-primary"
              >
                <i
                  aria-hidden="true"
                  className={cn(
                    'size-1.5 flex-none rounded-full bg-[#7b555d]',
                    tab.session.active && 'bg-primary shadow-[0_0_7px_#77f2b477]',
                  )}
                />
                <span className={cn(
                  'min-w-0 truncate text-[9px] font-semibold',
                  tab.preview && 'italic',
                )}>
                  {tabDeviceLabel} / {tab.terminalLabel}
                </span>
                {tab.preview && (
                  <span className="flex-none rounded border border-[#395247] bg-[#14241d] px-1 font-mono text-[7px] not-italic text-[#8fc8aa]">
                    预览
                  </span>
                )}
                <code className="flex-none font-mono text-[7px] text-[#60717d]">
                  {tab.session.id.slice(0, 6)}
                </code>
              </button>
              <button
                type="button"
                draggable={false}
                aria-label={`从窗格 ${paneNumber} 移除终端 ${tabDeviceLabel} ${tab.terminalLabel}，后台继续运行`}
                title={`从当前窗格移除，后台终端继续运行 · ${tab.session.id.slice(0, 8)}`}
                onClick={() => onDetachTab(tab.session.id)}
                onDragStart={(event) => event.preventDefault()}
                className="grid w-6 flex-none cursor-pointer place-items-center bg-transparent text-[#52616c] hover:bg-[#352027] hover:text-[#ff929d] focus-visible:outline-1 focus-visible:outline-[#ff929d] max-md:w-8"
              >
                <XIcon className="size-2.5" />
              </button>
            </span>
          )
        })}
        <span
          aria-hidden="true"
          data-terminal-drop-leaf-id={leafId}
          data-terminal-drop-index={tabs.length}
          data-terminal-drop-active={dropTarget?.leafId === leafId && dropTarget.index === tabs.length ? 'true' : 'false'}
          onDragOver={(event) => {
            if (!draggedTab) return
            event.preventDefault()
            event.stopPropagation()
            event.dataTransfer.dropEffect = 'move'
            onTabDragOver(event, tabs.length)
          }}
          onDrop={(event) => {
            if (!draggedTab) return
            event.preventDefault()
            event.stopPropagation()
            onTabDrop(event, tabs.length)
          }}
          className={cn(
            'relative flex min-w-8 flex-1 items-center px-3 font-mono text-[8px] text-[#5e6d78]',
            !tabs.length && 'min-w-24',
            dropTarget?.leafId === leafId && dropTarget.index === tabs.length && 'bg-[#14241d]',
          )}
        >
          {!tabs.length && '空窗格'}
          {dropTarget?.leafId === leafId && dropTarget.index === tabs.length && (
            <i aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-0 z-10 w-0.5 bg-primary shadow-[0_0_8px_#77f2b4]" />
          )}
        </span>
      </div>
      <div className="flex flex-none items-center gap-0.5 border-l border-[#26313a] bg-[#0f161c] px-1">
        {activePreviewTab && (
          <button
            type="button"
            onClick={() => onPinTab(activePreviewTab.session.id)}
            aria-label={`固定窗格 ${paneNumber} 的预览终端 ${activePreviewTab.terminalLabel}`}
            title="固定当前预览标签"
            className="grid size-6 cursor-pointer place-items-center rounded text-[#8fc8aa] hover:bg-[#193025] hover:text-primary max-md:size-8"
          >
            <Pin className="size-3" />
          </button>
        )}
        {tabs.length > 0 && (
          <button
            type="button"
            onClick={onNewTab}
            disabled={creatingTab}
            aria-label={`在窗格 ${paneNumber} 新建终端标签`}
            title="在当前窗格新建同设备、同工作区终端"
            className="grid size-6 cursor-pointer place-items-center rounded text-[#82918c] hover:bg-[#193025] hover:text-primary disabled:cursor-wait disabled:text-[#536159] disabled:hover:bg-transparent max-md:size-8"
          >
            {creatingTab ? <LoaderCircle className="size-3 animate-spin" /> : <Plus className="size-3.5" />}
          </button>
        )}
        {canSplit && (
          <>
            <button
              type="button"
              onClick={() => onSplit('row')}
              aria-label={`将窗格 ${paneNumber} 向右分屏，新窗格为空`}
              title="向右分屏（新窗格为空）"
              className="grid size-6 cursor-pointer place-items-center rounded text-[#82918c] hover:bg-[#193025] hover:text-primary max-md:size-8"
            >
              <Columns2 className="size-3.5" />
            </button>
            <button
              type="button"
              onClick={() => onSplit('column')}
              aria-label={`将窗格 ${paneNumber} 向下分屏，新窗格为空`}
              title="向下分屏（新窗格为空）"
              className="grid size-6 cursor-pointer place-items-center rounded text-[#82918c] hover:bg-[#193025] hover:text-primary max-md:size-8"
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
            className="grid size-6 cursor-pointer place-items-center rounded text-[#75838e] hover:bg-[#2b1b20] hover:text-[#ff9aa4] max-md:size-8"
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
    creatingTab,
    onFocusLeaf,
    onActivateTab,
    onPinTab,
    onDetachTab,
    onMoveTab,
    onNewTab,
    onRatio,
    onSplit,
    onClosePane,
    onPreviewService,
    onOpenWorkspace,
  } = props
  const rootRef = useRef<HTMLDivElement>(null)
  const draggedTabRef = useRef<DraggedPaneTab | null>(null)
  const suppressTabClickRef = useRef<string | null>(null)
  const [draggedTab, setDraggedTab] = useState<DraggedPaneTab | null>(null)
  const [dropTarget, setDropTarget] = useState<PaneDropTarget | null>(null)
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
          preview: leaf.previewTab === sessionId,
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

  const clearTabDrag = () => {
    const draggedSessionId = draggedTabRef.current?.sessionId ?? null
    draggedTabRef.current = null
    setDraggedTab(null)
    setDropTarget(null)
    if (draggedSessionId) {
      // Chromium may synthesize one click immediately after dragend. Limit the
      // guard to that event-loop turn so the next intentional click still works.
      window.setTimeout(() => {
        if (suppressTabClickRef.current === draggedSessionId) {
          suppressTabClickRef.current = null
        }
      }, 0)
    }
  }
  const startTabDrag = (
    event: React.DragEvent<HTMLButtonElement>,
    sessionId: string,
    sourceLeafId: string,
  ) => {
    const sourceLeaf = leafPlacements.find((placement) => placement.leaf.id === sourceLeafId)?.leaf
    if (!sourceLeaf?.tabs.includes(sessionId)) {
      event.preventDefault()
      return
    }
    const drag = { sessionId, sourceLeafId }
    draggedTabRef.current = drag
    suppressTabClickRef.current = sessionId
    setDraggedTab(drag)
    setDropTarget(null)
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData(TERMINAL_TAB_DRAG_TYPE, JSON.stringify(drag))
  }
  const updateTabDropTarget = (leafId: string, index: number) => {
    if (!draggedTabRef.current) return
    setDropTarget((current) => (
      current?.leafId === leafId && current.index === index ? current : { leafId, index }
    ))
  }
  const dropTab = (leafId: string, index: number) => {
    const drag = draggedTabRef.current
    if (!drag) return
    onMoveTab(drag.sessionId, drag.sourceLeafId, leafId, index)
    clearTabDrag()
  }

  useEffect(() => {
    if (!draggedTab) return
    const cancelWithEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      clearTabDrag()
    }
    window.addEventListener('keydown', cancelWithEscape, true)
    return () => window.removeEventListener('keydown', cancelWithEscape, true)
  }, [draggedTab])

  return (
    <div
      ref={rootRef}
      data-mounted-terminal-count={mountedSessions.length}
      data-terminal-tab-dragging={draggedTab ? 'true' : 'false'}
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
            data-terminal-pane-focused={focused ? 'true' : 'false'}
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
              leafId={leaf.id}
              paneNumber={paneNumber}
              tabs={tabs}
              activeTabId={activeTab?.session.id ?? null}
              focused={focused}
              draggedTab={draggedTab}
              dropTarget={dropTarget}
              canSplit={!singleMode}
              canClosePane={!singleMode && sashes.length > 0}
              creatingTab={creatingTab}
              onActivateTab={(sessionId, options) => {
                const suppressed = suppressTabClickRef.current
                if (suppressed === sessionId) {
                  suppressTabClickRef.current = null
                  return
                }
                onActivateTab(sessionId, leaf.id, options)
              }}
              onPinTab={(sessionId) => {
                const suppressed = suppressTabClickRef.current
                if (suppressed === sessionId) {
                  suppressTabClickRef.current = null
                  return
                }
                onPinTab(sessionId, leaf.id)
              }}
              onDetachTab={(sessionId) => onDetachTab(sessionId, leaf.id)}
              onTabDragStart={(event, sessionId) => startTabDrag(event, sessionId, leaf.id)}
              onTabDragOver={(_event, targetIndex) => updateTabDropTarget(leaf.id, targetIndex)}
              onTabDrop={(_event, targetIndex) => dropTab(leaf.id, targetIndex)}
              onTabDragEnd={clearTabDrag}
              onNewTab={() => { if (activeTab) onNewTab(activeTab.session, leaf.id) }}
              onSplit={(direction) => onSplit(leaf.id, direction)}
              onClosePane={() => onClosePane(leaf.id)}
            />
            {draggedTab && (
              <div
                data-terminal-pane-drop-zone={paneNumber}
                data-terminal-drop-leaf-id={leaf.id}
                data-terminal-drop-index={tabs.length}
                data-terminal-drop-active={dropTarget?.leafId === leaf.id && dropTarget.index === tabs.length ? 'true' : 'false'}
                onDragOver={(event) => {
                  event.preventDefault()
                  event.stopPropagation()
                  event.dataTransfer.dropEffect = 'move'
                  updateTabDropTarget(leaf.id, tabs.length)
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  event.stopPropagation()
                  dropTab(leaf.id, tabs.length)
                }}
                className={cn(
                  'pointer-events-auto absolute inset-x-0 top-[34px] bottom-0 z-30 grid place-items-center border-2 border-dashed border-transparent bg-[#07100db8] text-center transition-colors max-md:top-8',
                  dropTarget?.leafId === leaf.id && dropTarget.index === tabs.length
                    ? 'border-primary bg-[#10231bdb]'
                    : 'hover:border-[#426451]',
                )}
              >
                <span className="rounded border border-[#2c4939] bg-[#0c1712e8] px-3 py-2 font-mono text-[9px] text-[#8fc8aa] shadow-lg">
                  移动到窗格 P{paneNumber}
                </span>
              </div>
            )}
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
                    从上方设备组单击终端可临时预览，双击可固定为此窗格的标签。
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
