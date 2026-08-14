/**
 * VSCode 式 Editor Groups 布局树:二叉分屏树,叶子是窗格(leaf),
 * 每个 leaf 持有自己的标签列表(终端会话 id)和自己的 activeTab。
 * 一个会话 id 全树唯一 —— xterm 实例只能渲染一次。
 *
 * 本文件全部为无副作用纯函数:无操作时必须返回原引用,
 * 以便 React 通过引用比较跳过无谓的重渲染和持久化写入。
 */

export type LayoutDirection = 'row' | 'column'

export type LeafNode = {
  type: 'leaf'
  id: string
  tabs: string[]
  activeTab: string | null
}

export type SplitNode = {
  type: 'split'
  id: string
  direction: LayoutDirection
  /** 第一个子节点占据的比例,0.15–0.85 */
  ratio: number
  children: [LayoutNode, LayoutNode]
}

export type LayoutNode = LeafNode | SplitNode

export const MIN_RATIO = 0.15
export const MAX_RATIO = 0.85

// localStorage is untrusted, persistent input. Bounds keep a corrupt or
// hand-edited layout from causing excessive recursion/work during bootstrap.
const MAX_LAYOUT_DEPTH = 32
const MAX_LAYOUT_NODES = 255
const MAX_LAYOUT_SESSIONS = 512

let idCounter = 0

function nextId(prefix: string): string {
  idCounter += 1
  const random =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10)
  return `${prefix}-${idCounter}-${random}`
}

export function createLeaf(tabs: string[] = [], activeTab: string | null = null): LeafNode {
  return { type: 'leaf', id: nextId('leaf'), tabs, activeTab: activeTab ?? tabs[0] ?? null }
}

export function findLeaf(root: LayoutNode, leafId: string): LeafNode | null {
  if (root.type === 'leaf') return root.id === leafId ? root : null
  return findLeaf(root.children[0], leafId) ?? findLeaf(root.children[1], leafId)
}

export function leafOfSession(root: LayoutNode, sessionId: string): LeafNode | null {
  if (root.type === 'leaf') return root.tabs.includes(sessionId) ? root : null
  return leafOfSession(root.children[0], sessionId) ?? leafOfSession(root.children[1], sessionId)
}

export function firstLeaf(root: LayoutNode): LeafNode {
  return root.type === 'leaf' ? root : firstLeaf(root.children[0])
}

export function listLeaves(root: LayoutNode): LeafNode[] {
  if (root.type === 'leaf') return [root]
  return [...listLeaves(root.children[0]), ...listLeaves(root.children[1])]
}

export function listSessionIds(root: LayoutNode): string[] {
  if (root.type === 'leaf') return root.tabs
  return [...listSessionIds(root.children[0]), ...listSessionIds(root.children[1])]
}

function appendTabsToFirstLeaf(
  root: LayoutNode,
  tabs: string[],
): { root: LayoutNode; leafId: string } {
  if (root.type === 'leaf') {
    const existing = new Set(root.tabs)
    const additions = tabs.filter((tab) => {
      if (existing.has(tab)) return false
      existing.add(tab)
      return true
    })
    return {
      root: additions.length > 0 ? { ...root, tabs: [...root.tabs, ...additions] } : root,
      leafId: root.id,
    }
  }
  const appended = appendTabsToFirstLeaf(root.children[0], tabs)
  return {
    root: appended.root === root.children[0]
      ? root
      : { ...root, children: [appended.root, root.children[1]] },
    leafId: appended.leafId,
  }
}

/**
 * 关闭一个窗格但保留其中的会话。目标 leaf 的最近父 split 会坍缩，
 * 其全部标签追加到兄弟子树的第一个 leaf，且不改变接收 leaf 的 activeTab。
 * 根节点本身是唯一 leaf 或 leafId 无效时返回 null。
 */
export function closeLeaf(
  root: LayoutNode,
  leafId: string,
): { root: LayoutNode; focusedLeafId: string } | null {
  if (root.type === 'leaf') return null

  const closeInNode = (
    node: LayoutNode,
  ): { root: LayoutNode; focusedLeafId: string } | null => {
    if (node.type === 'leaf') return null
    const [first, second] = node.children

    if (first.type === 'leaf' && first.id === leafId) {
      const receiver = appendTabsToFirstLeaf(second, first.tabs)
      return { root: receiver.root, focusedLeafId: receiver.leafId }
    }
    if (second.type === 'leaf' && second.id === leafId) {
      const receiver = appendTabsToFirstLeaf(first, second.tabs)
      return { root: receiver.root, focusedLeafId: receiver.leafId }
    }

    const closedFirst = closeInNode(first)
    if (closedFirst) {
      return {
        root: { ...node, children: [closedFirst.root, second] },
        focusedLeafId: closedFirst.focusedLeafId,
      }
    }
    const closedSecond = closeInNode(second)
    if (closedSecond) {
      return {
        root: { ...node, children: [first, closedSecond.root] },
        focusedLeafId: closedSecond.focusedLeafId,
      }
    }
    return null
  }

  return closeInNode(root)
}

function setActiveTab(root: LayoutNode, leafId: string, sessionId: string): LayoutNode {
  if (root.type === 'leaf') {
    if (root.id !== leafId || !root.tabs.includes(sessionId)) return root
    if (root.activeTab === sessionId) return root
    return { ...root, activeTab: sessionId }
  }
  const first = setActiveTab(root.children[0], leafId, sessionId)
  const second = setActiveTab(root.children[1], leafId, sessionId)
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

/**
 * 从树中移除一个会话。所在 leaf 变空时整树坍缩:
 * 兄弟子树顶替父 split;整棵树空了返回 null。
 */
export function removeSession(root: LayoutNode, sessionId: string): LayoutNode | null {
  if (root.type === 'leaf') {
    const index = root.tabs.indexOf(sessionId)
    if (index < 0) return root
    const tabs = root.tabs.filter((tab) => tab !== sessionId)
    if (tabs.length === 0) return null
    const activeTab =
      root.activeTab === sessionId ? tabs[Math.min(index, tabs.length - 1)] : root.activeTab
    return { ...root, tabs, activeTab }
  }
  const first = removeSession(root.children[0], sessionId)
  const second = removeSession(root.children[1], sessionId)
  if (!first) return second
  if (!second) return first
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

function appendToLeaf(root: LayoutNode, leafId: string, sessionId: string, activate: boolean): LayoutNode {
  if (root.type === 'leaf') {
    if (root.id !== leafId) return root
    return {
      ...root,
      tabs: [...root.tabs, sessionId],
      activeTab: activate || !root.activeTab ? sessionId : root.activeTab,
    }
  }
  const first = appendToLeaf(root.children[0], leafId, sessionId, activate)
  const second = appendToLeaf(root.children[1], leafId, sessionId, activate)
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

export type ActivateResult = { root: LayoutNode; leafId: string }

/**
 * 让一个会话成为某个 leaf 的 activeTab,并返回应聚焦的 leaf。
 * - 会话已在树中:仅在原 leaf 激活,结构和归属不变。
 * - 会话不在树中(新建):追加到指定 leaf 或第一个 leaf。
 */
export function activateSession(
  root: LayoutNode | null,
  sessionId: string,
  options: { leafId?: string | null } = {},
): ActivateResult {
  if (!root) {
    const leaf = createLeaf([sessionId], sessionId)
    return { root: leaf, leafId: leaf.id }
  }
  const currentLeaf = leafOfSession(root, sessionId)
  const targetId = options.leafId && findLeaf(root, options.leafId) ? options.leafId : null
  if (currentLeaf) {
    return { root: setActiveTab(root, currentLeaf.id, sessionId), leafId: currentLeaf.id }
  }
  let next = root
  if (!next) {
    const leaf = createLeaf([sessionId], sessionId)
    return { root: leaf, leafId: leaf.id }
  }
  const leafId = targetId && findLeaf(next, targetId) ? targetId : firstLeaf(next).id
  next = appendToLeaf(next, leafId, sessionId, true)
  return { root: next, leafId }
}

/**
 * 把 leafId 沿 direction 一分为二:原会话留在原 leaf,
 * 新会话放进新 leaf(初始 50/50)。返回新 leaf 的 id。
 */
export function splitLeaf(
  root: LayoutNode,
  leafId: string,
  direction: LayoutDirection,
  newSessionId: string,
): { root: LayoutNode; newLeafId: string } {
  const newLeaf = createLeaf([newSessionId], newSessionId)
  const replace = (node: LayoutNode): LayoutNode => {
    if (node.type === 'leaf') {
      if (node.id !== leafId) return node
      return {
        type: 'split',
        id: nextId('split'),
        direction,
        ratio: 0.5,
        children: [node, newLeaf],
      }
    }
    const first = replace(node.children[0])
    const second = replace(node.children[1])
    if (first === node.children[0] && second === node.children[1]) return node
    return { ...node, children: [first, second] }
  }
  return { root: replace(root), newLeafId: newLeaf.id }
}

export function setRatio(root: LayoutNode, splitId: string, ratio: number): LayoutNode {
  const clamped = Math.min(MAX_RATIO, Math.max(MIN_RATIO, ratio))
  if (root.type === 'leaf') return root
  const first = setRatio(root.children[0], splitId, clamped)
  const second = setRatio(root.children[1], splitId, clamped)
  if (root.id === splitId) {
    if (Math.abs(root.ratio - clamped) < 0.0001) return root
    return { ...root, ratio: clamped }
  }
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

/**
 * 与服务器会话列表对账:剔除已消失的 id(空 leaf 自动坍缩),
 * 把树里没有的新 id 追加到第一个 leaf(不抢占 activeTab)。
 */
export function reconcile(root: LayoutNode | null, sessionIds: string[]): LayoutNode | null {
  let next: LayoutNode | null = root
  if (next) {
    for (const id of listSessionIds(next)) {
      if (!sessionIds.includes(id) && next) next = removeSession(next, id)
    }
  }
  if (!next) {
    if (sessionIds.length === 0) return null
    next = createLeaf([...sessionIds], sessionIds[0])
    return next
  }
  for (const id of sessionIds) {
    if (!leafOfSession(next, id)) {
      next = appendToLeaf(next, firstLeaf(next).id, id, false)
    }
  }
  return next
}

export type PersistedLayout = { version: 1; focusedLeafId: string | null; layout: LayoutNode }

export function serializeLayout(root: LayoutNode, focusedLeafId: string | null): string {
  const payload: PersistedLayout = { version: 1, focusedLeafId, layout: root }
  return JSON.stringify(payload)
}

type LayoutValidationState = {
  nodeIds: Set<string>
  leafIds: Set<string>
  sessionIds: Set<string>
  nodeCount: number
}

function isNonEmptyId(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function isValidNode(
  node: unknown,
  state: LayoutValidationState,
  depth = 0,
): node is LayoutNode {
  if (depth > MAX_LAYOUT_DEPTH || state.nodeCount >= MAX_LAYOUT_NODES) return false
  if (!node || typeof node !== 'object') return false
  const candidate = node as Record<string, unknown>
  if (!isNonEmptyId(candidate.id) || state.nodeIds.has(candidate.id)) return false
  state.nodeIds.add(candidate.id)
  state.nodeCount += 1
  if (candidate.type === 'leaf') {
    if (!Array.isArray(candidate.tabs) || candidate.tabs.length === 0) return false
    if (state.sessionIds.size + candidate.tabs.length > MAX_LAYOUT_SESSIONS) return false
    for (const tab of candidate.tabs) {
      if (!isNonEmptyId(tab) || state.sessionIds.has(tab)) return false
      state.sessionIds.add(tab)
    }
    if (!isNonEmptyId(candidate.activeTab) || !candidate.tabs.includes(candidate.activeTab)) {
      return false
    }
    state.leafIds.add(candidate.id)
    return true
  }
  if (candidate.type === 'split') {
    const children = candidate.children
    return (
      (candidate.direction === 'row' || candidate.direction === 'column') &&
      typeof candidate.ratio === 'number' &&
      Number.isFinite(candidate.ratio) &&
      candidate.ratio >= MIN_RATIO &&
      candidate.ratio <= MAX_RATIO &&
      Array.isArray(children) &&
      children.length === 2 &&
      isValidNode(children[0], state, depth + 1) &&
      isValidNode(children[1], state, depth + 1)
    )
  }
  return false
}

export function parseLayout(raw: string | null): PersistedLayout | null {
  if (!raw) return null
  try {
    const payload = JSON.parse(raw) as Partial<PersistedLayout>
    const validation: LayoutValidationState = {
      nodeIds: new Set(),
      leafIds: new Set(),
      sessionIds: new Set(),
      nodeCount: 0,
    }
    if (payload.version !== 1 || !isValidNode(payload.layout, validation)) return null
    const focusedLeafId =
      typeof payload.focusedLeafId === 'string' && validation.leafIds.has(payload.focusedLeafId)
        ? payload.focusedLeafId
        : null
    return {
      version: 1,
      focusedLeafId,
      layout: payload.layout,
    }
  } catch {
    return null
  }
}
