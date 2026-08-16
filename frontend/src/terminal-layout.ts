/**
 * VSCode 式 Editor Groups 布局树:二叉分屏树,叶子是窗格(leaf),
 * 每个 leaf 持有自己的标签列表(终端会话 id)、自己的 activeTab，
 * 以及至多一个可替换的 previewTab。previewTab 一定属于 tabs。
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
  /** VS Code 式临时预览标签；null 表示此窗格内所有标签都已固定。 */
  previewTab: string | null
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

export function createLeaf(
  tabs: string[] = [],
  activeTab: string | null = null,
  previewTab: string | null = null,
): LeafNode {
  return {
    type: 'leaf',
    id: nextId('leaf'),
    tabs,
    activeTab: activeTab ?? tabs[0] ?? null,
    previewTab: previewTab && tabs.includes(previewTab) ? previewTab : null,
  }
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

function firstLeafWithActiveTab(root: LayoutNode): LeafNode {
  return listLeaves(root).find((leaf) => leaf.activeTab) ?? firstLeaf(root)
}

export function listLeaves(root: LayoutNode): LeafNode[] {
  if (root.type === 'leaf') return [root]
  return [...listLeaves(root.children[0]), ...listLeaves(root.children[1])]
}

export function listSessionIds(root: LayoutNode): string[] {
  if (root.type === 'leaf') return root.tabs
  return [...listSessionIds(root.children[0]), ...listSessionIds(root.children[1])]
}

/**
 * 关闭一个窗格但不终止其中的后台会话。目标 leaf 的最近父 split 会
 * 坍缩；其中 Tabs 从布局解除归属，仍可通过设备导航/搜索重新打开。
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
      return { root: second, focusedLeafId: firstLeafWithActiveTab(second).id }
    }
    if (second.type === 'leaf' && second.id === leafId) {
      return { root: first, focusedLeafId: firstLeafWithActiveTab(first).id }
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
 * 从树中移除一个会话，但保留它所在的窗格。最后一个 Tab 被关闭后
 * leaf 变为空组；只有显式 closeLeaf 才会收缩分屏结构。
 */
export function removeSession(root: LayoutNode, sessionId: string): LayoutNode {
  if (root.type === 'leaf') {
    const index = root.tabs.indexOf(sessionId)
    if (index < 0) return root
    const tabs = root.tabs.filter((tab) => tab !== sessionId)
    const activeTab =
      root.activeTab === sessionId ? tabs[Math.min(index, tabs.length - 1)] ?? null : root.activeTab
    return {
      ...root,
      tabs,
      activeTab,
      previewTab: root.previewTab === sessionId ? null : root.previewTab,
    }
  }
  const first = removeSession(root.children[0], sessionId)
  const second = removeSession(root.children[1], sessionId)
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

function replaceLeaves(
  root: LayoutNode,
  replacements: ReadonlyMap<string, LeafNode>,
): LayoutNode {
  if (root.type === 'leaf') return replacements.get(root.id) ?? root
  const first = replaceLeaves(root.children[0], replacements)
  const second = replaceLeaves(root.children[1], replacements)
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

function normalizedInsertionIndex(value: number | undefined, length: number): number {
  if (value === undefined) return length
  return Math.min(length, Math.max(0, Math.trunc(value)))
}

/**
 * 显式拖动一个 Pane Tab。只改变布局归属/顺序，不改变后台终端。
 *
 * - 跨 Pane：源 Pane 保留（最后一个 Tab 移走后为空），目标 Pane 激活该 Tab。
 * - pinned 保持 pinned；preview 保持 preview，并替换目标 Pane 原有 preview。
 * - 同 Pane：按插入边界重排并激活，preview 标记保持不变。
 */
export function moveSession(
  root: LayoutNode,
  sessionId: string,
  options: {
    sourceLeafId: string
    targetLeafId: string
    targetIndex?: number
  },
): LayoutNode {
  if (options.targetIndex !== undefined && !Number.isFinite(options.targetIndex)) return root
  const source = findLeaf(root, options.sourceLeafId)
  const target = findLeaf(root, options.targetLeafId)
  if (!source || !target || !source.tabs.includes(sessionId)) return root

  const sourceIndex = source.tabs.indexOf(sessionId)
  if (source.id === target.id) {
    const tabs = source.tabs.filter((id) => id !== sessionId)
    let insertionIndex = normalizedInsertionIndex(options.targetIndex, source.tabs.length)
    if (insertionIndex > sourceIndex) insertionIndex -= 1
    insertionIndex = Math.min(tabs.length, Math.max(0, insertionIndex))
    tabs.splice(insertionIndex, 0, sessionId)
    const unchangedOrder = tabs.every((id, index) => id === source.tabs[index])
    if (unchangedOrder && source.activeTab === sessionId) return root
    return replaceLeaves(root, new Map([
      [source.id, { ...source, tabs, activeTab: sessionId }],
    ]))
  }

  const movingPreview = source.previewTab === sessionId
  const sourceTabs = source.tabs.filter((id) => id !== sessionId)
  const nextSource: LeafNode = {
    ...source,
    tabs: sourceTabs,
    activeTab: source.activeTab === sessionId
      ? sourceTabs[Math.min(sourceIndex, sourceTabs.length - 1)] ?? null
      : source.activeTab,
    previewTab: movingPreview ? null : source.previewTab,
  }

  let targetTabs = [...target.tabs]
  let targetPreview = target.previewTab
  let insertionIndex = normalizedInsertionIndex(options.targetIndex, targetTabs.length)
  if (movingPreview && targetPreview) {
    const displacedPreviewIndex = targetTabs.indexOf(targetPreview)
    targetTabs = targetTabs.filter((id) => id !== targetPreview)
    if (displacedPreviewIndex >= 0 && displacedPreviewIndex < insertionIndex) insertionIndex -= 1
    targetPreview = null
  }
  insertionIndex = Math.min(targetTabs.length, Math.max(0, insertionIndex))
  targetTabs.splice(insertionIndex, 0, sessionId)
  const nextTarget: LeafNode = {
    ...target,
    tabs: targetTabs,
    activeTab: sessionId,
    previewTab: movingPreview ? sessionId : targetPreview,
  }

  return replaceLeaves(root, new Map([
    [source.id, nextSource],
    [target.id, nextTarget],
  ]))
}

function placeInLeaf(
  root: LayoutNode,
  leafId: string,
  sessionId: string,
  mode: 'preview' | 'pinned',
): LayoutNode {
  if (root.type === 'leaf') {
    if (root.id !== leafId) return root
    const tabsWithoutPreview = root.previewTab
      ? root.tabs.filter((tab) => tab !== root.previewTab)
      : root.tabs
    const tabs = tabsWithoutPreview.includes(sessionId)
      ? tabsWithoutPreview
      : [...tabsWithoutPreview, sessionId]
    return {
      ...root,
      tabs,
      activeTab: sessionId,
      previewTab: mode === 'preview' ? sessionId : null,
    }
  }
  const first = placeInLeaf(root.children[0], leafId, sessionId, mode)
  const second = placeInLeaf(root.children[1], leafId, sessionId, mode)
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
  const leafId = targetId ?? firstLeaf(root).id
  const next = placeInLeaf(root, leafId, sessionId, 'pinned')
  return { root: next, leafId }
}

/**
 * 在 active editor group 中临时预览终端。
 *
 * - 目标窗格原有 preview 会被替换并重新成为未归属后台终端。
 * - 已固定在其他窗格的终端只会 reveal 原窗格，不会复制或暗中移动。
 * - 其他窗格中的临时 preview 可以移动到当前目标窗格。
 */
export function previewSession(
  root: LayoutNode | null,
  sessionId: string,
  options: { leafId?: string | null } = {},
): ActivateResult {
  const initialRoot = root ?? createLeaf()
  const targetLeafId = options.leafId && findLeaf(initialRoot, options.leafId)
    ? options.leafId
    : firstLeaf(initialRoot).id
  const owner = leafOfSession(initialRoot, sessionId)

  if (owner && owner.previewTab !== sessionId) {
    return {
      root: setActiveTab(initialRoot, owner.id, sessionId),
      leafId: owner.id,
    }
  }
  if (owner?.id === targetLeafId) {
    return {
      root: setActiveTab(initialRoot, owner.id, sessionId),
      leafId: owner.id,
    }
  }

  const withoutOldPreview = owner
    ? removeSession(initialRoot, sessionId)
    : initialRoot
  return {
    root: placeInLeaf(withoutOldPreview, targetLeafId, sessionId, 'preview'),
    leafId: targetLeafId,
  }
}

/**
 * 把终端固定为目标窗格的普通 Tab。双击已预览的终端会清除 preview
 * 标记；已固定在其他窗格的终端仍只 reveal 原归属窗格。
 */
export function pinSession(
  root: LayoutNode | null,
  sessionId: string,
  options: { leafId?: string | null } = {},
): ActivateResult {
  const initialRoot = root ?? createLeaf()
  const targetLeafId = options.leafId && findLeaf(initialRoot, options.leafId)
    ? options.leafId
    : firstLeaf(initialRoot).id
  const owner = leafOfSession(initialRoot, sessionId)

  if (owner && owner.previewTab !== sessionId) {
    return {
      root: setActiveTab(initialRoot, owner.id, sessionId),
      leafId: owner.id,
    }
  }
  const withoutOldPreview = owner && owner.id !== targetLeafId
    ? removeSession(initialRoot, sessionId)
    : initialRoot
  return {
    root: placeInLeaf(withoutOldPreview, targetLeafId, sessionId, 'pinned'),
    leafId: targetLeafId,
  }
}

/** 严格从指定窗格解除标签归属；后台终端生命周期不受影响。 */
export function detachSession(
  root: LayoutNode,
  leafId: string,
  sessionId: string,
): LayoutNode {
  const owner = leafOfSession(root, sessionId)
  if (!owner || owner.id !== leafId) return root
  return removeSession(root, sessionId)
}

/**
 * 把 leafId 沿 direction 一分为二：原窗格保持不变，新窗格默认为空。
 * 分屏只改变视图布局，不创建后台终端。
 */
export function splitLeaf(
  root: LayoutNode,
  leafId: string,
  direction: LayoutDirection,
): { root: LayoutNode; newLeafId: string } | null {
  if (!findLeaf(root, leafId)) return null
  const newLeaf = createLeaf()
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
 * 与服务器会话列表对账：只剔除已消失的 id，并保留空窗格。
 *
 * 布局只记录用户已经打开到各 Pane 的 Tabs。服务器新发现但尚未打开
 * 的后台会话必须继续留在设备导航/搜索中，不能自动灌入 P1。
 */
export function reconcile(root: LayoutNode | null, sessionIds: string[]): LayoutNode | null {
  let next = root
  if (!next) return null
  const validIds = new Set(sessionIds)
  for (const id of listSessionIds(next)) {
    if (!validIds.has(id)) next = removeSession(next, id)
  }
  return next
}

export type PersistedLayout = { version: 1; focusedLeafId: string | null; layout: LayoutNode }

/**
 * Preview 是运行时工作区状态，不是持久化标签。写入 storage 前将每个
 * leaf 的 preview 解除归属；双击 pin 后 previewTab 已为 null，才会保留。
 */
function withoutTransientPreviews(root: LayoutNode): LayoutNode {
  if (root.type === 'leaf') {
    if (!root.previewTab) return root
    const tabs = root.tabs.filter((tab) => tab !== root.previewTab)
    const activeTab = root.activeTab === root.previewTab
      ? tabs[tabs.length - 1] ?? null
      : root.activeTab
    return { ...root, tabs, activeTab, previewTab: null }
  }
  const first = withoutTransientPreviews(root.children[0])
  const second = withoutTransientPreviews(root.children[1])
  if (first === root.children[0] && second === root.children[1]) return root
  return { ...root, children: [first, second] }
}

export function serializeLayout(root: LayoutNode, focusedLeafId: string | null): string {
  const payload: PersistedLayout = {
    version: 1,
    focusedLeafId,
    layout: withoutTransientPreviews(root),
  }
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
    if (!Array.isArray(candidate.tabs)) return false
    if (state.sessionIds.size + candidate.tabs.length > MAX_LAYOUT_SESSIONS) return false
    for (const tab of candidate.tabs) {
      if (!isNonEmptyId(tab) || state.sessionIds.has(tab)) return false
      state.sessionIds.add(tab)
    }
    if (candidate.tabs.length === 0) {
      if (candidate.activeTab !== null) return false
    } else if (!isNonEmptyId(candidate.activeTab) || !candidate.tabs.includes(candidate.activeTab)) return false
    if (
      candidate.previewTab !== undefined &&
      candidate.previewTab !== null &&
      (!isNonEmptyId(candidate.previewTab) || !candidate.tabs.includes(candidate.previewTab))
    ) return false
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

/** v1 旧布局没有 previewTab；迁移时所有既有标签均视为已固定。 */
function normalizeParsedNode(node: LayoutNode): LayoutNode {
  if (node.type === 'leaf') return { ...node, previewTab: null }
  return {
    ...node,
    children: [normalizeParsedNode(node.children[0]), normalizeParsedNode(node.children[1])],
  }
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
      layout: normalizeParsedNode(payload.layout),
    }
  } catch {
    return null
  }
}
